-- Least-privilege login users for AutoTrader AI (run once per cluster as an admin).
--
--   psql "$ADMIN_URL" -v api_password='...' -v worker_password='...' -f infra/db/roles.sql
--
-- * autotrader_api    -> member of autotrader_reader (SELECT only)
-- * autotrader_worker -> member of autotrader_writer (INSERT/UPDATE on collector tables)
-- Neither can DELETE, TRUNCATE, ALTER, DROP, or touch system_config / audit_log.
-- Table grants themselves are applied by Alembic migration 0003 to the group roles.
\set ON_ERROR_STOP on

SELECT 'CREATE ROLE autotrader_reader NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'autotrader_reader') \gexec
SELECT 'CREATE ROLE autotrader_writer NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'autotrader_writer') \gexec

SELECT 'CREATE ROLE autotrader_api LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'autotrader_api') \gexec
SELECT 'CREATE ROLE autotrader_worker LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'autotrader_worker') \gexec

ALTER ROLE autotrader_api WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT
    PASSWORD :'api_password';
ALTER ROLE autotrader_worker WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT
    PASSWORD :'worker_password';

GRANT autotrader_reader TO autotrader_api;
GRANT autotrader_writer TO autotrader_worker;
