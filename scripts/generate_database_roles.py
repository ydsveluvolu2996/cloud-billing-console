import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
import django
django.setup()
from pathlib import Path
from billing.access import CUSTOMER_PATHS,ACCOUNT_PATHS
from django.apps import apps
models={m.__name__:m for m in apps.get_app_config('billing').get_models()}
sql='''-- Apply after migrations as the administration/schema owner, never a runtime role.
-- Runtime roles are never table owners or BYPASSRLS. Reapply after schema changes.
DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='billing_web') THEN CREATE ROLE billing_web LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS; END IF;
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='billing_collector') THEN CREATE ROLE billing_collector LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS; END IF;
END $$;
ALTER ROLE billing_web NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
ALTER ROLE billing_collector NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
DO $$ DECLARE membership record; BEGIN
 FOR membership IN SELECT parent.rolname AS parent, child.rolname AS child FROM pg_auth_members m JOIN pg_roles parent ON parent.oid=m.roleid JOIN pg_roles child ON child.oid=m.member WHERE child.rolname IN ('billing_web','billing_collector') LOOP
  EXECUTE format('REVOKE %I FROM %I',membership.parent,membership.child);
 END LOOP;
 IF EXISTS(SELECT 1 FROM pg_tables WHERE schemaname='public' AND tableowner IN ('billing_web','billing_collector')) THEN RAISE EXCEPTION 'Runtime table ownership must be transferred to the administration role first'; END IF;
END $$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM billing_web,billing_collector;
GRANT USAGE ON SCHEMA public TO billing_web,billing_collector;
CREATE OR REPLACE FUNCTION billing_can_access(cid uuid, aid text DEFAULT NULL, writing boolean DEFAULT false)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
 SELECT EXISTS (SELECT 1 FROM auth_user u LEFT JOIN billing_usersecurity s ON s.user_id=u.id
 WHERE u.id::text=current_setting('billing.user_id',true) AND u.is_active AND
 ((u.is_superuser AND s.portfolio_access AND NOT s.external) OR EXISTS
 (SELECT 1 FROM billing_customermembership m JOIN billing_customer c ON c.id=m.customer_id
 WHERE m.user_id=u.id AND m.customer_id=cid AND m.active AND c.active AND (m.expires_at IS NULL OR m.expires_at>now()) AND
 (NOT writing OR m.role='operator') AND (NOT COALESCE(s.external,false) OR (m.role='customer' AND current_setting('billing.external_enabled',true)='true')) AND (m.role<>'customer' OR current_setting('billing.external_enabled',true)='true') AND
 (m.account_ids='[]'::jsonb OR (aid IS NOT NULL AND m.account_ids ? aid)))))
$$;
REVOKE ALL ON FUNCTION billing_can_access(uuid,text,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing_can_access(uuid,text,boolean) TO billing_web;
'''
cases=[]
for name,path in CUSTOMER_PATHS.items():
 m=models[name]; alias='t0';curr=m;joins=[];i=0
 for part in path.split('__'):
  if part=='pk':expr=f'{alias}.{curr._meta.pk.column}';break
  if part.endswith('_id'):expr=f'{alias}.{part}';break
  f=curr._meta.get_field(part);i+=1; nxt=f't{i}';joins.append(f'JOIN {f.related_model._meta.db_table} {nxt} ON {alias}.{f.column}={nxt}.{f.target_field.column}');curr=f.related_model;alias=nxt
 cases.append(f" WHEN '{m._meta.db_table}' THEN RETURN (SELECT {expr} FROM {m._meta.db_table} t0 {' '.join(joins)} WHERE t0.{m._meta.pk.column}::text=rid);")
sql+='''CREATE OR REPLACE FUNCTION billing_parent_customer(tab text,rid text) RETURNS uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$ BEGIN CASE tab
'''+ '\n'.join(cases)+"\n ELSE RETURN NULL; END CASE; END $$;\nREVOKE ALL ON FUNCTION billing_parent_customer(text,text) FROM PUBLIC;\nGRANT EXECUTE ON FUNCTION billing_parent_customer(text,text) TO billing_web;\n"
sql+='''CREATE OR REPLACE FUNCTION billing_account_number(rid bigint) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$ SELECT account_id FROM billing_awsaccount WHERE id=rid $$;
REVOKE ALL ON FUNCTION billing_account_number(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing_account_number(bigint) TO billing_web;
CREATE OR REPLACE FUNCTION billing_account_access(aid text,writing boolean) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT EXISTS (SELECT 1 FROM billing_accountassignment a JOIN billing_awsaccount x ON x.id=a.account_id WHERE x.account_id=aid AND billing_can_access(a.customer_id,aid,writing))
$$;
REVOKE ALL ON FUNCTION billing_account_access(text,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing_account_access(text,boolean) TO billing_web;
'''
sql+="""
CREATE OR REPLACE FUNCTION billing_customer_visible(cid uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT billing_can_access(cid,NULL,false) OR EXISTS(SELECT 1 FROM billing_customermembership m, jsonb_array_elements_text(m.account_ids) a WHERE m.customer_id=cid AND m.user_id::text=current_setting('billing.user_id',true) AND billing_can_access(cid,a,false))
$$;
CREATE OR REPLACE FUNCTION billing_source_visible(sid uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT EXISTS(SELECT 1 FROM billing_billingsource s WHERE s.id=sid AND billing_can_access(s.customer_id,NULL,false)) OR EXISTS(SELECT 1 FROM billing_accountassignment a JOIN billing_awsaccount x ON x.id=a.account_id WHERE x.source_id=sid AND billing_can_access(a.customer_id,x.account_id,false))
$$;
CREATE OR REPLACE FUNCTION billing_revoke_customer_access(cid uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$ BEGIN
 IF NOT billing_can_access(cid,NULL,true) THEN RAISE EXCEPTION 'Customer access denied'; END IF;
 UPDATE billing_customer SET active=false,offboarded_at=now() WHERE id=cid;
 UPDATE billing_usersecurity SET session_version=session_version+1 WHERE user_id IN(SELECT user_id FROM billing_customermembership WHERE customer_id=cid);
 UPDATE billing_customermembership SET active=false WHERE customer_id=cid;
END $$;
REVOKE ALL ON FUNCTION billing_customer_visible(uuid),billing_source_visible(uuid),billing_revoke_customer_access(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing_customer_visible(uuid),billing_source_visible(uuid),billing_revoke_customer_access(uuid) TO billing_web;
"""
collector_write={'Cost','AwsAccount','AccountAssignment','CollectionPeriod','SyncRun','Job','ExplorerQuery','ProjectCost','BudgetEvaluation','ImportedBudget','Alert','OperationalAlert'}
web_write={'Customer','BillingSource','AwsAccount','AccountAssignment','Project','AllocationRule','Budget','BudgetAmount','AllianceRecord','AllianceServiceNote','SavedReport','BulkImport','Job','ExplorerQuery','ProjectCost','BudgetEvaluation','Alert','CustomerApproval','RoleApproval','RolloutReadiness','OffboardingRecord','OperationalAlert','AlertRoute','PortalInvitation','ReconciliationRun'}
for name,m in models.items():
 table=m._meta.db_table
 if name in ('CustomerMembership','UserSecurity'):
  continue
 if name=='AwsAccount':
  read="billing_account_access(account_id,false) OR billing_can_access(billing_parent_customer('billing_billingsource',source_id::text),NULL,false)"
  write="billing_account_access(account_id,true) OR billing_can_access(billing_parent_customer('billing_billingsource',source_id::text),NULL,true)"
 elif name=='BulkImport':
  read="requested_by_id::text=current_setting('billing.user_id',true) AND scope_fingerprint<>'' AND scope_fingerprint=current_setting('billing.scope_fingerprint',true)";write=read
 elif name in CUSTOMER_PATHS:
  path=CUSTOMER_PATHS[name];first=path.split('__')[0]
  if '__' not in path:
   cid='id' if path=='pk' else path
  else:
   f=m._meta.get_field(first);cid=f"billing_parent_customer('{f.related_model._meta.db_table}',{f.column}::text)"
  apath=ACCOUNT_PATHS.get(name)
  aid='NULL'
  if apath and '__' not in apath:aid=apath
  elif apath=='account__account_id':aid='billing_account_number(account_id)'
  elif name in ('AllianceServiceNote','AllianceRevision'):aid='(SELECT billing_account_number(account_id) FROM billing_alliancerecord WHERE id=record_id)'
  read=f'billing_can_access({cid},{aid},false)';write=f'billing_can_access({cid},{aid},true)'
  if name=='Customer':read='billing_customer_visible(id)'
  if name=='BillingSource':read='billing_source_visible(id)'
  if name=='Job':
   scoped_request="kind='explorer_refresh' AND EXISTS(SELECT 1 FROM billing_explorerquery q WHERE q.source_id=billing_job.source_id AND q.requested_by_id::text=current_setting('billing.user_id',true))"
   read=f'({read}) OR ({scoped_request})';write=f'({write}) OR ({scoped_request})'
  if name=='ExplorerQuery':
   read=f'billing_customer_visible({cid})'
   read+=" AND requested_by_id::text=current_setting('billing.user_id',true) AND scope_fingerprint<>'' AND scope_fingerprint=current_setting('billing.scope_fingerprint',true)"
   write=read
  if name=='AuditEvent':
   write="actor=(SELECT username FROM auth_user WHERE id::text=current_setting('billing.user_id',true)) OR current_setting('billing.user_id',true) IS NULL OR current_setting('billing.user_id',true)=''"
  if name=='SavedReport':
   read=f"billing_customer_visible({cid}) AND created_by=(SELECT username FROM auth_user WHERE id::text=current_setting('billing.user_id',true))";write=read
  if name=='ExplorerQuery':write=read # view-only users may request their scoped reports
 else:continue
 # NULL customer/account cannot match a membership, so this is true only for
 # the existing active, internal portfolio-admin branch. An uncorrelated
 # subquery runs once per statement (including each prepared execution), and
 # CASE avoids evaluating the per-row permission function for that admin.
 # Keep scoped fallbacks, write checks and user-owned report/cache policies.
 if name in ('Customer','Cost'):
  read=f'CASE WHEN (SELECT billing_can_access(NULL,NULL,false)) THEN true ELSE ({read}) END'
 sql+=f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;\nDROP POLICY IF EXISTS web_scope ON {table};\nCREATE POLICY web_scope ON {table} TO billing_web USING ({read}) WITH CHECK ({write});\n'
 sql+=f'DROP POLICY IF EXISTS collector_scope ON {table};\nCREATE POLICY collector_scope ON {table} TO billing_collector USING (true) WITH CHECK (true);\n'
 sql+=f'GRANT SELECT ON {table} TO billing_web;\n'
 if name in web_write:sql+=f'GRANT INSERT,UPDATE,DELETE ON {table} TO billing_web;\n'
 if name=='AllianceRevision':sql+=f'GRANT INSERT ON {table} TO billing_web;\n'
 if name=='AuditEvent':sql+=f'GRANT INSERT ON {table} TO billing_web,billing_collector;\n'
 if name not in ('UserSecurity','PortalInvitation','BulkImport'):
  sql+=f'GRANT SELECT ON {table} TO billing_collector;\n'
 if name in collector_write:sql+=f'GRANT INSERT,UPDATE,DELETE ON {table} TO billing_collector;\n'
sql+='''GRANT SELECT ON billing_customermembership,billing_usersecurity,auth_user TO billing_web;
GRANT SELECT ON billing_customermembership TO billing_collector;
GRANT SELECT(id,user_id,portfolio_access,external,session_version) ON billing_usersecurity TO billing_collector;
GRANT SELECT(id,username,is_active,is_superuser,is_staff) ON auth_user TO billing_collector;
GRANT SELECT,INSERT,UPDATE,DELETE ON django_session,axes_accessattempt,axes_accessattemptexpiration,axes_accesslog,axes_accessfailurelog,otp_totp_totpdevice TO billing_web;
GRANT SELECT ON auth_group,auth_permission,auth_user_groups,auth_user_user_permissions,auth_group_permissions,django_content_type TO billing_web;
GRANT UPDATE(password,last_login) ON auth_user TO billing_web;
GRANT INSERT ON billing_usersecurity TO billing_web;
GRANT UPDATE(session_version,recovery_hashes,recovery_failed,recovery_locked_until) ON billing_usersecurity TO billing_web;
GRANT UPDATE(ownership_version,capabilities,trust_checks,discovery_mode,onboarding_step,sync_requested,initial_import_done,verified_at,discovered_at,last_attempt,last_success,last_error,next_run) ON billing_billingsource TO billing_collector;
REVOKE UPDATE,DELETE ON billing_customerapproval,billing_roleapproval FROM billing_web;
GRANT UPDATE(contacts,authorized_users,expected_accounts,billing_fields,metadata,optional_capabilities,storage_region,retention_days,evidence,updated_at) ON billing_customerapproval TO billing_web;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO billing_web,billing_collector;
-- Schema ownership and auth grants must be checked with the actual runtime logins.
'''
# New profile rows cannot bootstrap portfolio or OIDC privileges.
sql+='''CREATE OR REPLACE FUNCTION billing_guard_profile() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF current_user='billing_web' AND (NEW.portfolio_access OR NEW.oidc_subject<>'' OR NEW.oidc_issuer<>'') THEN RAISE EXCEPTION 'Administrative profile grants require the admin identity'; END IF;
 RETURN NEW; END $$;
DROP TRIGGER IF EXISTS guard_profile ON billing_usersecurity;
CREATE TRIGGER guard_profile BEFORE INSERT ON billing_usersecurity FOR EACH ROW EXECUTE FUNCTION billing_guard_profile();
'''
sql += """
CREATE OR REPLACE FUNCTION billing_guard_approval() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF current_user='billing_web' AND (NEW.status NOT IN ('pending','requested') OR NEW.approved_by<>'' OR NEW.approved_at IS NOT NULL) THEN RAISE EXCEPTION 'Approval requires administration identity'; END IF;
 RETURN NEW; END $$;
DROP TRIGGER IF EXISTS guard_customer_approval ON billing_customerapproval;
CREATE TRIGGER guard_customer_approval BEFORE INSERT OR UPDATE ON billing_customerapproval FOR EACH ROW EXECUTE FUNCTION billing_guard_approval();
DROP TRIGGER IF EXISTS guard_role_approval ON billing_roleapproval;
CREATE TRIGGER guard_role_approval BEFORE INSERT OR UPDATE ON billing_roleapproval FOR EACH ROW EXECUTE FUNCTION billing_guard_approval();
"""
sql += Path('deploy/ownership.sql').read_text()
sql += Path('deploy/readiness.sql').read_text()
sql += Path('deploy/portal-admission.sql').read_text()
sql += Path('deploy/user-administration.sql').read_text()
Path('deploy/database-roles.sql').write_text(sql)
