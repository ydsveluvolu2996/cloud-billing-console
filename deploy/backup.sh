#!/usr/bin/env bash
set -euo pipefail
cd /opt/cloud-billing
umask 077
task_backup=$(mktemp /tmp/cloud-billing-backup.XXXXXX)
trap 'rm -f "$task_backup"' EXIT
docker compose exec -T db pg_dump -U billing -d billing -Fc > "$task_backup"
test -s "$task_backup"
task_bucket=$(sed -n 's/^ARTIFACT_BUCKET=//p' .env)
aws s3 cp "$task_backup" "s3://${task_bucket}/backups/billing-$(date -u +%Y%m%dT%H%M%SZ).dump" --sse AES256 --only-show-errors
# Database and Django secret are both required for complete disaster recovery.
aws s3 cp .env "s3://${task_bucket}/backups/config.env" --sse AES256 --only-show-errors
printf 'Backup completed at %s\n' "$(date -u +%FT%TZ)"
