# Changelog

All notable changes to the Oogway Discord Bot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Complete test suite with pytest and pytest-asyncio
- Unit tests for `chi.py` prediction engine (12 tests)
- Unit tests for async `RiotClient` (15 tests)
- Test coverage reporting with pytest-cov
- Alembic database migrations system
- Initial database migration capturing current schema
- Centralized logging configuration in `oogway/logging_config.py`
- Health check endpoints (`/health`, `/readiness`, `/liveness`, `/metrics`)
- GitHub Actions CI/CD pipeline with testing, linting, Docker build, and security scanning
- `.env.example` template file with all required environment variables
- `IMPROVEMENTS.md` documenting all code improvements
- `CHANGELOG.md` for tracking version changes
- New configuration options: `JOIN_PING_ROLE_ID`, `DEFAULT_REGION`, `LOG_LEVEL`
- Custom exception classes: `RiotAPIError` and `RateLimitError`
- Type hints and docstrings for all public functions

### Changed
- **BREAKING**: `RiotClient` completely rewritten to be fully async using `aiohttp`
- **BREAKING**: All `RiotClient` methods now return `Optional` types to handle 404 responses
- Replaced all `time.sleep()` with `asyncio.sleep()` for proper async behavior
- Improved exception handling: replaced bare `except Exception` with specific exception types
- Database sessions now use context managers everywhere for proper cleanup
- Logging now centralized through `oogway/logging_config.py`
- Docker container now runs as non-root user `oogway` (UID 1000)
- Dockerfile optimized with better caching and security practices
- Updated `.gitignore` with comprehensive exclusions
- Requirements updated with `aiohttp`, `Pillow`, and testing dependencies

### Fixed
- Event loop blocking due to synchronous `time.sleep()` in async context
- Potential database connection leaks from improper session management
- Missing `await` keywords on Riot API calls in `link.py`
- Hardcoded role IDs now moved to configuration

### Removed
- `run_in_executor` wrappers for Riot API calls (now natively async)
- Duplicate logging configurations across modules

## [1.0.0] - 2026-01-12

### Initial Release
- Discord bot for League of Legends players
- Account linking with Riot Games API
- Match tracking and notifications
- OogScore calculation system
- Custom 5v5 game management
- Draft system with captain picks
- Leaderboard system
- Event scheduling with RSVP
- Profile viewing
- Champion meta tracking
- Redis caching
- SQLite database
- Docker deployment support
