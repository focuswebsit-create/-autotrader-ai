-- Least-privilege login users for AutoTrader AI (run once per cluster as an admin).
--
--   psql "$ADMIN_URL" -v api_password='...' -v worker_password='...' \
--        -v control_password='...' -v paper_password='...' -f infra/db/roles.sql
--
-- * autotrader_api     -> member of autotrader_reader (SELECT only)
-- * autotrader_worker  -> member of autotrader_writer (INSERT/UPDATE on collector tables)
-- * autotrader_paper   -> member of autotrader_trader (the paper-trader: INSERT on paper
--                         tables + the writer rights it needs for funding / heartbeats)
-- * autotrader_control -> member of autotrader_controller (Phase 3 Control API: INSERT
--                         paper_commands / audit_log / auth sessions, revoke sessions)
-- None can DELETE, TRUNCATE, ALTER, DROP, or touch system_config.
-- Table grants themselves are applied by Alembic migrations (0003, 0009) to the group
-- roles. Safe to re-run (idempotent) after an upgrade.
\set ON_ERROR_STOP on

SELECT 'CREATE ROLE autotrader_reader NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'autotrader_reader') \gexec
SELECT 'CREATE ROLE autotrader_writer NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'autotrader_writer') \gexec

SELECT 'CREATE ROLE autotrader_controller NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'autotrader_controller') \gexec

SELECT 'CREATE ROLE autotrader_trader NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'autotrader_trader') \gexec

SELECT 'CREATE ROLE autotrader_api LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'autotrader_api') \gexec
SELECT 'CREATE ROLE autotrader_worker LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'autotrader_worker') \gexec
SELECT 'CREATE ROLE autotrader_paper LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'autotrader_paper') \gexec
SELECT 'CREATE ROLE autotrader_control LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'autotrader_control') \gexec

ALTER ROLE autotrader_api WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT
    PASSWORD :'api_password';
ALTER ROLE autotrader_worker WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT
    PASSWORD :'worker_password';

ALTER ROLE autotrader_paper WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    INHERIT PASSWORD :'paper_password';
ALTER ROLE autotrader_control WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    INHERIT PASSWORD :'control_password';

GRANT autotrader_reader TO autotrader_api;
GRANT autotrader_writer TO autotrader_worker;
GRANT autotrader_controller TO autotrader_control;
GRANT autotrader_trader TO autotrader_paper;
-- (migration 0009 makes autotrader_trader a member of autotrader_writer)
