"""
Module principal du bot de trading
Version améliorée avec WebSocket et gestion robuste
"""

import asyncio
import json
import signal
import sys
from typing import Dict, List, Optional, Set
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
import os

from .logger import log_info, log_error, log_debug, log_warning, log_trade
from .multi_pair_manager import MultiPairManager
from .watchlist_scanner import WatchlistScanner
from .weighted_score_engine import WeightedScoreEngine
from .risk_manager import RiskManager
from .market_data import MarketData
from .websocket_market_feed import WebSocketMarketFeed, DataType, MarketUpdate
from ..strategies.strategy import MultiSignalStrategy
from ..exchanges.exchange_connector import ExchangeConnector
from config.settings import load_config, validate_config


class BotState(Enum):
    """États possibles du bot"""
    INITIALIZING = "initializing"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class BotStatus:
    """Status complet du bot"""
    state: BotState
    start_time: datetime
    last_update: datetime
    total_trades: int = 0
    open_positions: int = 0
    total_pnl: float = 0.0
    daily_pnl: float = 0.0
    errors: List[str] = field(default_factory=list)
    metrics: Dict = field(default_factory=dict)


class TradingBot:
    """Bot de trading principal avec architecture événementielle"""
    
    def __init__(self, config_path: str = "config/config.json", paper_trading: bool = True):
        """
        Initialise le bot de trading
        
        Args:
            config_path: Chemin vers le fichier de configuration
            paper_trading: Mode paper trading (défaut: True)
        """
        # Configuration
        self.config = self._load_and_validate_config(config_path)
        self.paper_trading = paper_trading
        
        # État du bot
        self.status = BotStatus(
            state=BotState.INITIALIZING,
            start_time=datetime.now(),
            last_update=datetime.now()
        )
        
        # Composants principaux
        self.exchange: Optional[ExchangeConnector] = None
        self.websocket_feed: Optional[WebSocketMarketFeed] = None
        self.watchlist_scanner: Optional[WatchlistScanner] = None
        self.risk_manager: Optional[RiskManager] = None
        self.market_data: Optional[MarketData] = None
        self.pair_manager: Optional[MultiPairManager] = None
        
        # Contrôle d'exécution
        self._main_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        self._tasks: Set[asyncio.Task] = set()
        
        # Gestion des signaux système
        self._setup_signal_handlers()
        
        log_info(f"TradingBot initialisé - Mode: {'PAPER' if paper_trading else 'LIVE'}")
    
    def _load_and_validate_config(self, config_path: str) -> Dict:
        """Charge et valide la configuration"""
        try:
            config = load_config(config_path)
            
            # Ajouter les clés d'environnement si nécessaire
            if not config['exchange'].get('api_key'):
                config['exchange']['api_key'] = os.getenv('BINANCE_API_KEY', '')
            if not config['exchange'].get('api_secret'):
                config['exchange']['api_secret'] = os.getenv('BINANCE_API_SECRET', '')
            
            # Valider la configuration
            if not validate_config(config):
                raise ValueError("Configuration invalide")
            
            return config
            
        except Exception as e:
            log_error(f"Erreur lors du chargement de la config: {str(e)}")
            raise
    
    def _setup_signal_handlers(self):
        """Configure les gestionnaires de signaux système"""
        def signal_handler(signum, frame):
            log_warning(f"Signal {signum} reçu, arrêt en cours...")
            asyncio.create_task(self.shutdown())
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    
    async def initialize(self) -> bool:
        """
        Initialise tous les composants du bot
        
        Returns:
            True si l'initialisation réussit
        """
        try:
            log_info("Initialisation des composants...")
            
            # 1. Exchange connector
            self.exchange = ExchangeConnector(
                exchange_name=self.config['exchange']['name'],
                testnet=self.config['exchange']['testnet'],
                skip_connection=self.config['exchange'].get('skip_connection', False)
            )
            
            if not await self.exchange.connect(
                api_key=self.config['exchange']['api_key'],
                api_secret=self.config['exchange']['api_secret']
            ):
                raise Exception("Échec de connexion à l'exchange")
            
            # 2. WebSocket feed
            self.websocket_feed = WebSocketMarketFeed(
                exchange=self.config['exchange']['name'],
                testnet=self.config['exchange']['testnet']
            )
            
            if not await self.websocket_feed.connect():
                raise Exception("Échec de connexion WebSocket")
            
            # 3. Risk Manager
            self.risk_manager = RiskManager(self.config['risk_management'])
            
            # 4. Market Data Manager
            self.market_data = MarketData(
                exchange_connector=self.exchange,
                config=self.config.get('market_data', {})
            )
            
            # 5. Watchlist Scanner
            self.watchlist_scanner = WatchlistScanner(
                exchange_connector=self.exchange,
                min_volume_usdt=self.config['trading'].get('min_volume_usdt', 1_000_000),
                top_n=self.config['trading'].get('max_pairs', 10)
            )
            
            # 6. Multi-Pair Manager
            self.pair_manager = MultiPairManager(
                exchange=self.exchange,
                config=self.config,
                paper_trading=self.paper_trading
            )
            
            # Initialiser les données de marché
            initial_pairs = self.config['trading']['pairs']
            await self.market_data.initialize(initial_pairs)
            
            # S'abonner aux flux WebSocket
            await self._setup_websocket_subscriptions(initial_pairs)
            
            self.status.state = BotState.STOPPED
            log_info("✅ Tous les composants initialisés avec succès")
            
            return True
            
        except Exception as e:
            log_error(f"Erreur lors de l'initialisation: {str(e)}")
            self.status.state = BotState.ERROR
            self.status.errors.append(str(e))
            return False
    
    async def _setup_websocket_subscriptions(self, symbols: List[str]):
        """Configure les abonnements WebSocket"""
        for symbol in symbols:
            # S'abonner aux données nécessaires
            self.websocket_feed.subscribe(
                symbol=symbol,
                data_types=[DataType.TICKER, DataType.TRADES, DataType.ORDERBOOK],
                callback=self._handle_market_update
            )
        
        log_info(f"Abonné aux flux WebSocket pour {len(symbols)} paires")
    
    async def _handle_market_update(self, update: MarketUpdate):
        """
        Traite les mises à jour du marché en temps réel
        
        Args:
            update: Mise à jour reçue du WebSocket
        """
        try:
            # Mettre à jour les données en cache
            if update.data_type == DataType.TICKER:
                # Mise à jour rapide du ticker
                self.market_data.ticker_cache[update.symbol] = update.data
                
            elif update.data_type == DataType.ORDERBOOK:
                # Mise à jour du carnet d'ordres
                self.market_data._update_orderbook(update.symbol, update.data)
            
            # Vérifier la latence
            if update.latency_ms > 200:
                log_warning(f"Latence élevée détectée: {update.latency_ms:.1f}ms sur {update.symbol}")
            
            # Incrémenter les métriques
            self.status.metrics['ws_updates'] = self.status.metrics.get('ws_updates', 0) + 1
            
        except Exception as e:
            log_error(f"Erreur traitement update {update.symbol}: {str(e)}")
    
    async def start(self):
        """Démarre le bot de trading"""
        if self.status.state not in [BotState.STOPPED, BotState.ERROR]:
            log_warning(f"Impossible de démarrer, état actuel: {self.status.state}")
            return
        
        log_info("🚀 Démarrage du bot de trading...")
        self.status.state = BotState.RUNNING
        self.status.start_time = datetime.now()
        
        # Créer la tâche principale
        self._main_task = asyncio.create_task(self._main_loop())
        
        # Attendre l'arrêt
        await self._shutdown_event.wait()
    
    async def _main_loop(self):
        """Boucle principale du bot"""
        log_info("Boucle principale démarrée")
        
        # Créer les tâches parallèles
        tasks = [
            self._create_monitored_task(self._market_scanner_task(), "Scanner"),
            self._create_monitored_task(self._strategy_loop(), "Strategy"),
            self._create_monitored_task(self._risk_monitor_loop(), "Risk Monitor"),
            self._create_monitored_task(self._performance_tracker_loop(), "Performance"),
            self._create_monitored_task(self._health_check_loop(), "Health Check")
        ]
        
        try:
            # Attendre que toutes les tâches se terminent
            await asyncio.gather(*tasks)
        except Exception as e:
            log_error(f"Erreur dans la boucle principale: {str(e)}")
            self.status.state = BotState.ERROR
        finally:
            log_info("Boucle principale terminée")
    
    def _create_monitored_task(self, coro, name: str) -> asyncio.Task:
        """Crée une tâche surveillée"""
        async def monitored():
            try:
                await coro
            except asyncio.CancelledError:
                log_info(f"Tâche {name} annulée")
                raise
            except Exception as e:
                log_error(f"Erreur dans tâche {name}: {str(e)}")
                self.status.errors.append(f"{name}: {str(e)}")
        
        task = asyncio.create_task(monitored())
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._tasks.discard(t))
        return task
    
    async def _market_scanner_task(self):
        """Tâche de scan du marché"""
        while not self._shutdown_event.is_set():
            try:
                log_debug("Scan du marché en cours...")
                
                # Mettre à jour la watchlist
                await self.watchlist_scanner.update_watchlist()
                
                # Mettre à jour les données de marché
                await self.market_data.update_all()
                
                # Attendre avant le prochain scan
                await asyncio.sleep(self.config['trading'].get('scan_interval', 300))
                
            except Exception as e:
                log_error(f"Erreur dans market scanner: {str(e)}")
                await asyncio.sleep(60)  # Attendre 1 minute en cas d'erreur
    
    async def _strategy_loop(self):
        """Boucle d'exécution des stratégies"""
        interval = self.config.get('strategy_interval', 60)  # 1 minute par défaut
        
        while self.status.state == BotState.RUNNING:
            try:
                # Mettre à jour les données
                await self.pair_manager.update_market_data()
                
                # Vérifier les signaux
                signals = await self.pair_manager.check_signals()
                
                if signals:
                    log_info(f"📊 {len(signals)} signaux détectés")
                    
                    # Exécuter les signaux
                    await self.pair_manager.execute_signals(signals)
                
                # Mettre à jour les métriques
                self.status.open_positions = len(self.pair_manager.positions)
                self.status.last_update = datetime.now()
                
                # Attendre avant la prochaine itération
                await asyncio.sleep(interval)
                
            except Exception as e:
                log_error(f"Erreur dans strategy loop: {str(e)}")
                await asyncio.sleep(10)
    
    async def _risk_monitor_loop(self):
        """Boucle de surveillance des risques"""
        interval = 30  # 30 secondes
        
        while self.status.state == BotState.RUNNING:
            try:
                # Récupérer les métriques de risque
                capital = self.config['trading']['initial_balance']
                risk_metrics = self.risk_manager.get_risk_metrics(capital)
                
                # Vérifier les limites
                if risk_metrics.current_drawdown > 0.15:  # 15% drawdown
                    log_warning(f"⚠️ Drawdown élevé: {risk_metrics.current_drawdown:.1%}")
                    
                    if risk_metrics.current_drawdown > 0.20:  # 20% = limite critique
                        log_error("🚨 DRAWDOWN CRITIQUE - Arrêt du trading")
                        await self._pause_trading()
                
                if risk_metrics.daily_pnl > 0.05:  # Perte quotidienne > 5%
                    log_warning(f"⚠️ Perte quotidienne élevée: {risk_metrics.daily_pnl:.1%}")
                
                # Mettre à jour les positions avec trailing stops
                for symbol, position in self.pair_manager.positions.items():
                    ticker = self.websocket_feed.get_ticker(symbol)
                    if ticker:
                        new_stop = self.risk_manager.update_trailing_stop(
                            position, ticker['last']
                        )
                        if new_stop:
                            position['stop_loss'] = new_stop
                            log_debug(f"Trailing stop mis à jour pour {symbol}: {new_stop:.2f}")
                
                await asyncio.sleep(interval)
                
            except Exception as e:
                log_error(f"Erreur dans risk monitor: {str(e)}")
                await asyncio.sleep(60)
    
    async def _performance_tracker_loop(self):
        """Boucle de suivi des performances"""
        interval = 300  # 5 minutes
        
        while self.status.state == BotState.RUNNING:
            try:
                # Calculer les performances
                perf = self.pair_manager.get_performance_summary()
                
                # Mettre à jour le status
                self.status.total_trades = perf['total_trades']
                self.status.total_pnl = perf['total_pnl']
                
                # Calculer le PnL quotidien
                daily_pnl = self._calculate_daily_pnl()
                self.status.daily_pnl = daily_pnl
                
                # Logger les performances
                if perf['total_trades'] > 0:
                    log_info(
                        f"📈 Performance - Trades: {perf['total_trades']} | "
                        f"Win Rate: {perf['win_rate']:.1f}% | "
                        f"PnL: {perf['total_pnl']:+.2f} USDT | "
                        f"Daily: {daily_pnl:+.2f} USDT"
                    )
                
                # Sauvegarder l'état si nécessaire
                if self.config.get('save_state', True):
                    await self._save_state()
                
                await asyncio.sleep(interval)
                
            except Exception as e:
                log_error(f"Erreur dans performance tracker: {str(e)}")
                await asyncio.sleep(interval)
    
    async def _health_check_loop(self):
        """Boucle de vérification de santé"""
        interval = 60  # 1 minute
        
        while self.status.state == BotState.RUNNING:
            try:
                # Vérifier la connexion WebSocket
                ws_metrics = self.websocket_feed.get_metrics()
                if not ws_metrics['connected']:
                    log_error("WebSocket déconnecté, tentative de reconnexion...")
                    await self.websocket_feed.connect()
                
                # Vérifier la latence moyenne
                if ws_metrics['avg_latency_ms'] > 200:
                    log_warning(f"Latence moyenne élevée: {ws_metrics['avg_latency_ms']:.0f}ms")
                
                # Vérifier l'exchange
                if not self.exchange.connected:
                    log_error("Exchange déconnecté, tentative de reconnexion...")
                    await self.exchange.connect()
                
                # Nettoyer les erreurs anciennes
                if len(self.status.errors) > 100:
                    self.status.errors = self.status.errors[-50:]
                
                # Réinitialiser les compteurs quotidiens si nouveau jour
                await self._check_daily_reset()
                
                await asyncio.sleep(interval)
                
            except Exception as e:
                log_error(f"Erreur dans health check: {str(e)}")
                await asyncio.sleep(interval)
    
    async def _add_pair_to_watch(self, symbol: str):
        """Ajoute une paire à surveiller"""
        try:
            # Initialiser les données
            await self.market_data.initialize([symbol])
            
            # S'abonner au WebSocket
            self.websocket_feed.subscribe(
                symbol=symbol,
                data_types=[DataType.TICKER, DataType.TRADES],
                callback=self._handle_market_update
            )
            
            # Créer la stratégie
            strategy = MultiSignalStrategy(symbol)
            self.pair_manager.strategies[symbol] = strategy
            
            log_info(f"✅ {symbol} ajouté à la surveillance")
            
        except Exception as e:
            log_error(f"Erreur ajout {symbol}: {str(e)}")
    
    async def _pause_trading(self):
        """Met en pause le trading (garde la surveillance active)"""
        self.status.state = BotState.PAUSED
        log_warning("Trading mis en pause")
        
        # Fermer toutes les positions si configuré
        if self.config.get('close_on_pause', False):
            await self.pair_manager.close_all_positions("Protection drawdown")
    
    def _calculate_daily_pnl(self) -> float:
        """Calcule le PnL du jour"""
        # TODO: Implémenter le calcul basé sur l'historique
        # Pour l'instant, retourner le PnL des dernières 24h
        perf = self.pair_manager.performance
        daily_pnl = 0.0
        
        for symbol, data in perf.items():
            if data['last_trade']:
                time_since = datetime.now() - data['last_trade']
                if time_since < timedelta(days=1):
                    # Approximation : prendre une portion du PnL total
                    # TODO: Améliorer avec un vrai suivi journalier
                    daily_pnl += data['pnl'] * 0.1
        
        return daily_pnl
    
    async def _check_daily_reset(self):
        """Vérifie et effectue le reset quotidien si nécessaire"""
        now = datetime.now()
        if hasattr(self, '_last_daily_reset'):
            if now.date() > self._last_daily_reset.date():
                log_info("🔄 Reset quotidien des compteurs")
                self.risk_manager.reset_daily_counters()
                self._last_daily_reset = now
        else:
            self._last_daily_reset = now
    
    async def _save_state(self):
        """Sauvegarde l'état actuel du bot"""
        try:
            if not self.pair_manager:
                return
                
            state = {
                'timestamp': datetime.now().isoformat(),
                'state': self.status.state.value,
                'total_trades': self.status.total_trades,
                'open_positions': self.status.open_positions,
                'total_pnl': self.status.total_pnl,
                'daily_pnl': self.status.daily_pnl,
                'positions': self.pair_manager.get_positions(),
                'errors': self.status.errors[-10:]  # Garder les 10 dernières erreurs
            }
            
            state_file = Path('data/bot_state.json')
            state_file.parent.mkdir(parents=True, exist_ok=True)
            
            with open(state_file, 'w') as f:
                json.dump(state, f, indent=2)
                
            log_debug("État du bot sauvegardé")
            
        except Exception as e:
            log_error(f"Erreur sauvegarde état: {str(e)}")
    
    async def shutdown(self):
        """Arrête proprement le bot"""
        try:
            log_info("🛑 Arrêt du bot en cours...")
            self.status.state = BotState.STOPPING
            
            # Sauvegarder l'état final
            await self._save_state()
            
            # Fermer les connexions
            if self.websocket_feed:
                await self.websocket_feed.disconnect()
            
            if self.exchange:
                await self.exchange.close()
            
            self.status.state = BotState.STOPPED
            log_info("✅ Bot arrêté proprement")
            
        except Exception as e:
            log_error(f"Erreur lors de l'arrêt: {str(e)}")
            self.status.state = BotState.ERROR
        finally:
            self._shutdown_event.set()
    
    def get_status(self) -> Dict:
        """Retourne l'état actuel du bot"""
        uptime = datetime.now() - self.status.start_time
        
        # Métriques WebSocket
        ws_metrics = {}
        if self.websocket_feed:
            ws_metrics = self.websocket_feed.get_metrics()
        
        # Performance
        perf = {}
        if self.pair_manager:
            perf = self.pair_manager.get_performance_summary()
        
        return {
            'state': self.status.state.value,
            'uptime': str(uptime),
            'start_time': self.status.start_time.isoformat(),
            'last_update': self.status.last_update.isoformat(),
            'paper_trading': self.paper_trading,
            'trading': {
                'total_trades': self.status.total_trades,
                'open_positions': self.status.open_positions,
                'total_pnl': self.status.total_pnl,
                'daily_pnl': self.status.daily_pnl,
                'performance': perf
            },
            'websocket': ws_metrics,
            'errors': self.status.errors[-10:] if self.status.errors else [],
            'config': {
                'exchange': self.config['exchange']['name'],
                'pairs': len(self.pair_manager.strategies) if self.pair_manager else 0,
                'strategy': self.config['strategy']['name']
            }
        }