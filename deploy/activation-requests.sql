-- The HTTP role may submit intent only through this fixed, audited capability.
-- Direct request writes and all approval changes remain administration-only.
REVOKE INSERT,UPDATE,DELETE ON billing_activationrequest FROM billing_web,billing_collector;
CREATE OR REPLACE FUNCTION billing_request_activation(sid uuid, version integer, consent jsonb, region text, collector_arn text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE actor_id integer; actor_name text; s billing_billingsource; a billing_customerapproval;
 request_id uuid; existing billing_activationrequest; config jsonb; approval jsonb; role_approval jsonb; normalized jsonb; days integer;
BEGIN
 SELECT u.id,u.username INTO actor_id,actor_name FROM auth_user u JOIN billing_usersecurity p ON p.user_id=u.id
 WHERE u.id::text=current_setting('billing.user_id',true) AND u.is_active AND u.is_superuser
 AND p.portfolio_access AND NOT p.external AND p.session_version=version FOR UPDATE OF u,p;
 IF actor_id IS NULL OR current_setting('billing.mfa_verified',true) IS DISTINCT FROM 'true' THEN
  RAISE EXCEPTION 'MFA-verified portfolio administrator required' USING ERRCODE='42501';
 END IF;
 SELECT b.* INTO s FROM billing_billingsource b JOIN billing_customer c ON c.id=b.customer_id
 WHERE b.id=sid AND b.enabled AND c.active FOR UPDATE OF b,c;
 IF s.id IS NULL OR s.shared OR s.kind NOT IN ('standalone','member_budgets') THEN
  RAISE EXCEPTION 'Choose an active single-account or member budget connection';
 END IF;
 IF s.account_id !~ '^[0-9]{12}$' OR s.role_arn NOT LIKE 'arn:aws:iam::'||s.account_id||':role/BillingConsole/%'
 OR s.role_arn ~ '[*?[:space:]]' OR s.role_arn !~ '/[A-Za-z0-9_+=,.@-]{1,64}$' OR s.role_arn=collector_arn
 OR length(s.role_arn)>2048 THEN RAISE EXCEPTION 'Use the exact account role under BillingConsole/'; END IF;
 IF jsonb_typeof(s.approved_capabilities) IS DISTINCT FROM 'array'
 OR EXISTS(SELECT 1 FROM jsonb_array_elements_text(s.approved_capabilities) c WHERE c NOT IN
 ('organizations','tags','cost_categories','forecasts','comparison_drivers','resources','budgets'))
 OR (s.kind='standalone' AND s.approved_capabilities ? 'organizations')
 OR (s.kind='member_budgets' AND s.approved_capabilities<>'["budgets"]'::jsonb) THEN
  RAISE EXCEPTION 'Select only supported, account-scoped permissions'; END IF;
 IF jsonb_typeof(consent) IS DISTINCT FROM 'object' OR consent->'confirmed' IS DISTINCT FROM 'true'::jsonb
 OR jsonb_typeof(consent->'contact') IS DISTINCT FROM 'string'
 OR jsonb_typeof(consent->'evidence') IS DISTINCT FROM 'string'
 OR length(btrim(consent->>'contact')) NOT BETWEEN 1 AND 250
 OR length(btrim(consent->>'evidence')) NOT BETWEEN 1 AND 500
 OR jsonb_typeof(consent->'retention_days') IS DISTINCT FROM 'number'
 OR (consent->>'retention_days') !~ '^[0-9]{1,4}$'
 OR region IS NULL OR region !~ '^[a-z]{2}-[a-z]+-[0-9]+$' THEN RAISE EXCEPTION 'Complete the account consent fields'; END IF;
 days:=(consent->>'retention_days')::integer;
 IF days NOT BETWEEN 1 AND 3650 THEN RAISE EXCEPTION 'Choose a retention period between 1 and 3650 days'; END IF;
 normalized:=jsonb_build_object('contact',btrim(consent->>'contact'),'evidence',btrim(consent->>'evidence'),
  'retention_days',days,'confirmed',true);
 SELECT * INTO a FROM billing_customerapproval WHERE customer_id=s.customer_id FOR UPDATE;
 IF a.status='revoked' THEN RAISE EXCEPTION 'Revoked customer approval requires administration review'; END IF;
 IF a.status='approved' AND (a.storage_region<>region OR a.retention_days IS DISTINCT FROM days
 OR a.contacts='[]'::jsonb OR a.authorized_users='[]'::jsonb OR a.billing_fields='[]'::jsonb
 OR a.evidence='' OR a.approved_by='' OR a.approved_at IS NULL) THEN
  RAISE EXCEPTION 'Use the existing approved customer storage and retention settings'; END IF;
 approval:=CASE WHEN a.id IS NULL THEN 'null'::jsonb ELSE jsonb_build_object(
  'contacts',a.contacts,'authorized_users',a.authorized_users,'expected_accounts',a.expected_accounts,
  'billing_fields',a.billing_fields,'metadata',a.metadata,'optional_capabilities',a.optional_capabilities,
  'storage_region',a.storage_region,'retention_days',a.retention_days,'status',a.status,'evidence',a.evidence,'approved_by',a.approved_by) END;
 SELECT jsonb_build_object('status',r.status,'evidence',r.evidence,'approved_by',r.approved_by,'requested_by',r.requested_by)
 INTO role_approval FROM billing_roleapproval r WHERE r.source_id=s.id AND r.role_arn=s.role_arn AND r.connection_version=s.connection_version FOR UPDATE;
 IF role_approval->>'status'='revoked' THEN RAISE EXCEPTION 'Revoked role approval requires administration review'; END IF;
 config:=jsonb_build_object('source_id',s.id::text,'customer_id',s.customer_id::text,'account_id',s.account_id,
  'role_arn',s.role_arn,'external_id_sha256',encode(sha256(convert_to(s.external_id,'UTF8')),'hex'),
  'connection_version',s.connection_version,'kind',s.kind,'shared',s.shared,
  'approved_capabilities',COALESCE((SELECT jsonb_agg(c ORDER BY c) FROM (SELECT DISTINCT jsonb_array_elements_text(s.approved_capabilities) c) items),'[]'::jsonb));
 SELECT * INTO existing FROM billing_activationrequest WHERE source_id=s.id AND status IN ('queued','processing','verifying','importing');
 IF existing.id IS NOT NULL THEN
  IF existing.requested_by_id=actor_id AND existing.session_version=version AND existing.snapshot->'source'=config
   AND existing.snapshot->'consent'=normalized THEN RETURN existing.id; END IF;
  RAISE EXCEPTION 'A connection request is already running';
 END IF;
 request_id:=gen_random_uuid();
 INSERT INTO billing_activationrequest(id,source_id,requested_by_id,session_version,connection_version,snapshot,status,attempts,last_error,created_at,updated_at)
 VALUES(request_id,s.id,actor_id,version,s.connection_version,jsonb_build_object('source',config,'approval',approval,'role_approval',COALESCE(role_approval,'null'::jsonb),'consent',normalized,'region',region),
  'queued',0,'',now(),now());
 INSERT INTO billing_auditevent(at,actor,action,customer_id,source_id,details)
 VALUES(now(),actor_name,'Account activation requested',s.customer_id,s.id,
  jsonb_build_object('outcome','success','target',request_id::text,'account_id',s.account_id,'connection_version',s.connection_version));
 RETURN request_id;
END $$;
REVOKE ALL ON FUNCTION billing_request_activation(uuid,integer,jsonb,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing_request_activation(uuid,integer,jsonb,text,text) TO billing_web;
