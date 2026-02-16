# oogle_database.py – Gestion de la base de données pour OOGLE
import sqlite3
from pathlib import Path
from typing import Optional, List, Dict, Tuple
import datetime as dt
from zoneinfo import ZoneInfo

TZ_PARIS = ZoneInfo("Europe/Paris")


class OogleDatabase:
    """Gestion de la base de données pour les statistiques OOGLE."""
    
    def __init__(self, db_path: str = "data/oogle.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        """Initialise les tables si elles n'existent pas."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Table des parties jouées
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS oogle_games (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    won INTEGER NOT NULL,
                    word TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    UNIQUE(user_id, date)
                )
            """)
            
            # Table des statistiques globales par joueur
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS oogle_stats (
                    user_id INTEGER PRIMARY KEY,
                    total_games INTEGER DEFAULT 0,
                    total_wins INTEGER DEFAULT 0,
                    current_streak INTEGER DEFAULT 0,
                    max_streak INTEGER DEFAULT 0,
                    avg_attempts REAL DEFAULT 0.0,
                    distribution TEXT DEFAULT '{"1":0,"2":0,"3":0,"4":0,"5":0,"6":0}',
                    last_played TEXT
                )
            """)
            
            # Table des notifications (qui veut être pingé)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS oogle_notifications (
                    user_id INTEGER PRIMARY KEY,
                    enabled INTEGER DEFAULT 1
                )
            """)
            
            # Index pour améliorer les performances
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_oogle_games_date 
                ON oogle_games(date)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_oogle_games_user 
                ON oogle_games(user_id)
            """)
            
            conn.commit()
    
    def save_game(self, user_id: int, date: str, attempts: int, won: bool, word: str):
        """Enregistre une partie terminée et met à jour les statistiques."""
        import json
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            timestamp = dt.datetime.now(TZ_PARIS).isoformat()
            
            # Insérer ou remplacer la partie
            cursor.execute("""
                INSERT OR REPLACE INTO oogle_games 
                (user_id, date, attempts, won, word, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, date, attempts, 1 if won else 0, word, timestamp))
            
            # Récupérer les stats actuelles
            cursor.execute("""
                SELECT total_games, total_wins, current_streak, max_streak, 
                       distribution, last_played
                FROM oogle_stats WHERE user_id = ?
            """, (user_id,))
            row = cursor.fetchone()
            
            if row:
                total_games, total_wins, current_streak, max_streak, dist_json, last_played = row
                distribution = json.loads(dist_json)
            else:
                total_games = total_wins = current_streak = max_streak = 0
                distribution = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0, "6": 0}
                last_played = None
            
            # Mettre à jour les compteurs
            total_games += 1
            if won:
                total_wins += 1
                distribution[str(attempts)] = distribution.get(str(attempts), 0) + 1
                
                # Gérer le streak
                if last_played:
                    last_date = dt.datetime.fromisoformat(last_played).date()
                    current_date = dt.datetime.fromisoformat(timestamp).date()
                    days_diff = (current_date - last_date).days
                    
                    if days_diff == 1:
                        current_streak += 1
                    elif days_diff > 1:
                        current_streak = 1
                else:
                    current_streak = 1
                
                max_streak = max(max_streak, current_streak)
            else:
                current_streak = 0
            
            # Calculer la moyenne
            cursor.execute("""
                SELECT AVG(attempts) FROM oogle_games 
                WHERE user_id = ? AND won = 1
            """, (user_id,))
            avg_attempts = cursor.fetchone()[0] or 0.0
            
            # Mettre à jour les stats
            cursor.execute("""
                INSERT OR REPLACE INTO oogle_stats
                (user_id, total_games, total_wins, current_streak, max_streak, 
                 avg_attempts, distribution, last_played)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (user_id, total_games, total_wins, current_streak, max_streak,
                  avg_attempts, json.dumps(distribution), timestamp))
            
            conn.commit()
    
    def get_user_stats(self, user_id: int) -> Optional[Dict]:
        """Récupère les statistiques d'un joueur."""
        import json
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT total_games, total_wins, current_streak, max_streak,
                       avg_attempts, distribution, last_played
                FROM oogle_stats WHERE user_id = ?
            """, (user_id,))
            row = cursor.fetchone()
            
            if not row:
                return None
            
            return {
                "total_games": row[0],
                "total_wins": row[1],
                "win_rate": (row[1] / row[0] * 100) if row[0] > 0 else 0,
                "current_streak": row[2],
                "max_streak": row[3],
                "avg_attempts": row[4],
                "distribution": json.loads(row[5]),
                "last_played": row[6]
            }
    
    def get_leaderboard_streaks(self, limit: int = 10) -> List[Tuple[int, int]]:
        """Top des streaks actuels."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_id, current_streak 
                FROM oogle_stats 
                WHERE current_streak > 0
                ORDER BY current_streak DESC, max_streak DESC
                LIMIT ?
            """, (limit,))
            return cursor.fetchall()
    
    def get_leaderboard_max_streaks(self, limit: int = 10) -> List[Tuple[int, int]]:
        """Top des meilleurs streaks de tous les temps."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_id, max_streak 
                FROM oogle_stats 
                WHERE max_streak > 0
                ORDER BY max_streak DESC, current_streak DESC
                LIMIT ?
            """, (limit,))
            return cursor.fetchall()
    
    def get_leaderboard_best_avg(self, limit: int = 10, min_games: int = 5) -> List[Tuple[int, float]]:
        """Top des meilleures moyennes (minimum de parties jouées)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_id, avg_attempts 
                FROM oogle_stats 
                WHERE total_wins >= ? AND avg_attempts > 0
                ORDER BY avg_attempts ASC, total_wins DESC
                LIMIT ?
            """, (min_games, limit))
            return cursor.fetchall()
    
    def get_leaderboard_win_rate(self, limit: int = 10, min_games: int = 5) -> List[Tuple[int, int, int]]:
        """Top des meilleurs taux de victoire."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_id, total_wins, total_games
                FROM oogle_stats 
                WHERE total_games >= ?
                ORDER BY (CAST(total_wins AS REAL) / total_games) DESC, total_wins DESC
                LIMIT ?
            """, (min_games, limit))
            return cursor.fetchall()
    
    def get_leaderboard_total_wins(self, limit: int = 10) -> List[Tuple[int, int]]:
        """Top du nombre total de victoires."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_id, total_wins 
                FROM oogle_stats 
                WHERE total_wins > 0
                ORDER BY total_wins DESC, total_games ASC
                LIMIT ?
            """, (limit,))
            return cursor.fetchall()
    
    def get_today_completions(self, date: str) -> List[int]:
        """Récupère les user_ids qui ont terminé le Oogle du jour."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_id FROM oogle_games WHERE date = ?
            """, (date,))
            return [row[0] for row in cursor.fetchall()]
    
    def set_notification(self, user_id: int, enabled: bool):
        """Active ou désactive les notifications pour un utilisateur."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO oogle_notifications (user_id, enabled)
                VALUES (?, ?)
            """, (user_id, 1 if enabled else 0))
            conn.commit()
    
    def get_games_by_date(self, date: str) -> List[Dict]:
        """Récupère toutes les parties d'une date donnée."""
        cursor = self.conn.execute("""
            SELECT user_id, attempts, won FROM oogle_games
            WHERE date = ?
        """, (date,))
        
        results = []
        for row in cursor.fetchall():
            results.append({
                'user_id': row[0],
                'attempts': row[1],
                'won': bool(row[2])
            })
        return results

    def get_notification_status(self, user_id: int) -> bool:
        """Vérifie si un utilisateur a activé les notifications."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT enabled FROM oogle_notifications WHERE user_id = ?
            """, (user_id,))
            row = cursor.fetchone()
            return bool(row[0]) if row else False
    
    def get_all_notification_users(self) -> List[int]:
        """Récupère tous les user_ids qui ont activé les notifications."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_id FROM oogle_notifications WHERE enabled = 1
            """)
            return [row[0] for row in cursor.fetchall()]
