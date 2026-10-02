# TODO: 5and8 Ingest Dashboard + Pipeline

Status as of 2026-10-02. Website runs locally; nothing deployed to Rocky yet.

## 1. Do first (blocking / security)

- [ ] **Rotate the ShotGrid API keys** in ShotGrid. The old key is in `vfx-ingest-pipeline` git history; removing it from code does not remove it from history.
- [ ] Put the new keys in `/etc/vfx-ingest/sg.env` (chmod 600, owner sgd): `SG_URL`, `SG_SCRIPT_NAME`, `SG_API_KEY`, `SGDAEMON_DELIVERY_KEY`, `SGDAEMON_PACKAGE_NAME`, `SGDAEMON_PACKAGE_KEY`.
- [ ] Pipeline repo: `git mv tools/logs/package_worker.py tools/package_worker.py` (the package-worker service expects it there).
- [ ] Pipeline repo: delete the 4 empty `db/ingest.db-shm`, `db/ingest.db-wal`, `ingest.db-shm`, `ingest.db-wal`.

## 2. Commit / push

- [ ] Commit pipeline changes on `dev_20260802` (worker fixes, queue functions, env-var keys, `package-worker.service`, `test_web_queue.py`), then merge to `main`.
- [ ] Push website changes to GitHub `5and8-ingest` (row Retry, Services strip, Start/Restart, mail alerts, restart log, central config).

## 3. Deploy to Rocky

- [ ] Check DB location: `df -T /software`. If NFS, move `ingest.db` and `package_queue.db` to local disk (`/var/lib/vfx-ingest/`) and update `config/ingest.yaml` + `PACKAGE_QUEUE_DB`.
- [ ] Deploy pipeline changes first; restart `vfx-ingest-worker` (re-queues the stuck jobs #113, #796).
- [ ] Install `systemd/package-worker.service`, enable it.
- [ ] Add `EnvironmentFile=/etc/vfx-ingest/sg.env` to the event daemon unit (already in repo copy) and restart the daemon.
- [ ] Clone website to `/software/pipeline/5and8-ingest`.
- [ ] `ingest_venv/bin/pip install fastapi uvicorn itsdangerous ldap3`.
- [ ] Create `/etc/vfx-ingest/.ingest-web.yaml` (chmod 600, owner sgd) with a new `secret_key`, `admins`, new strong `temp_users` passwords, `alert_emails`, `restart_notify`.
- [ ] TLS cert + key from studio CA at `/etc/vfx-ingest/tls/{cert,key}.pem`.
- [ ] Sudoers rule `/etc/sudoers.d/vfx-ingest-web` (exact 4 restart commands, see README), `visudo -c`.
- [ ] `usermod -aG systemd-journal sgd` (service logs on the site).
- [ ] Install + enable `vfx-ingest-web.service`; open port 8443 in firewalld for the studio LAN.

## 4. Verify on Rocky

- [ ] Drop a test plate in `/jobs/DataIO/Inbox/<proj>/SEQ001/SH010/`, watch pending → validating → copying → verifying → completed on the site.
- [ ] Break a folder name, see the failure, Edit + Retry, confirm it completes.
- [ ] Set a test shot to `pkg`, watch it in Packaging; force a failure, Retry it.
- [ ] Services strip shows real state + journal lines for all 4 services.
- [ ] Admin Start/Restart works; non-admin gets refused.
- [ ] Mail: restart mail reaches jitesh@5and8.ai, `service_restarts.log` line written; stop a service and confirm the down/up alert; fail a job and confirm the alert.

## 5. Login

- [ ] Wire AD login: deploy ShotDeck at `SHOTDECK_ROOT`, confirm `auth_config.yml` (base DN, `CN=Users` vs `OU=Users`, CA cert for LDAPS).
- [ ] Test an `ai-users` member (allowed) and a non-member (denied).
- [ ] Remove `temp_users` from `.ingest-web.yaml`; set `admins` to AD usernames.
- [ ] Delete `D:\dev\5and8\5and8-ingest-temp-logins.md` once temp logins are gone.

## 6. Open decisions

- [ ] Package Retry: set ShotGrid status back to `pkg` so SG doesn't show `pkgerr` while it re-runs? Needs an SG script key on the web server.
- [ ] Remote daemon restart via `daemon_host` (IP/username): build with an SSH key limited to the one restart command, not a stored password.

## 7. Roadmap (Phase 2+)

- [ ] Freelancer send/receive + shot delivery plugins onto the `package_queue` pattern (daemon enqueues, worker copies) so they show on the site.
- [ ] Log rotation in `core/logger.py` (`RotatingFileHandler`).
- [ ] Watcher: cache known SG sequences instead of querying every 30 s.
- [ ] Daily digest mail to producers.
- [ ] Re-ingest a *completed* job (needs moving source back from `_ingested`).
- [ ] Copy progress % per job.
- [ ] Pipeline repo cleanup: untrack `ingest_venv/`, `db/*.db`, `ingest.db`, `tools/build/`, `tools/dist/`, `tools/logs/*.log`; remove `core/sg_hlper-V02.py`, `*_v002.py`, `plugins/test.py` once confirmed unused.
- [ ] Move PySide tools (delivery config designer, package config creator) to web pages.
