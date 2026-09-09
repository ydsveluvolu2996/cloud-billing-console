#!/usr/bin/env bash
set -euo pipefail
cd /opt/cloud-billing
umask 077
ARTIFACT_BUCKET=$(cat .deployment/backup-bucket)
[[ "$ARTIFACT_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || exit 1
task_dir=$(mktemp -d)
trap 'rm -rf "$task_dir"; unset PGPASSWORD' EXIT
export PGPASSWORD
PGPASSWORD=$(cat .deployment/secrets/admin_db_password)
docker compose exec -T -e PGPASSWORD db pg_dump -U billing_admin -d billing -Fc > "$task_dir/billing.dump"
test -s "$task_dir/billing.dump"
docker compose exec -T db pg_restore --list < "$task_dir/billing.dump" > "$task_dir/catalog.txt"
sha256sum "$task_dir/billing.dump" | awk '{print $1}' > "$task_dir/billing.sha256"
task_stamp=$(date -u +%Y%m%dT%H%M%SZ)
aws s3 cp "$task_dir/billing.dump" "s3://${ARTIFACT_BUCKET}/backups/${task_stamp}/billing.dump" --sse AES256 --only-show-errors
aws s3 cp "$task_dir/billing.sha256" "s3://${ARTIFACT_BUCKET}/backups/${task_stamp}/billing.sha256" --sse AES256 --only-show-errors
# Restore configuration from approved Secrets Manager versions; do not copy runtime secrets into a shared env backup.
printf 'Backup validated and uploaded at %s\n' "$task_stamp"
