-- Boolean/configuration-only helpers keep shared-payer approval documents private.
CREATE OR REPLACE FUNCTION billing_customer_configuration(cid uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT COALESCE(jsonb_object_agg(s.id::text,jsonb_build_array(s.connection_version,s.ownership_version)),'{}'::jsonb)
 FROM billing_billingsource s WHERE s.customer_id=cid OR EXISTS(
 SELECT 1 FROM billing_accountassignment a JOIN billing_awsaccount x ON x.id=a.account_id WHERE a.customer_id=cid AND a."end" IS NULL AND x.source_id=s.id)
$$;
CREATE OR REPLACE FUNCTION billing_connection_ready(sid uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT EXISTS(SELECT 1 FROM billing_billingsource s JOIN billing_customer c ON c.id=s.customer_id
 JOIN billing_customerapproval a ON a.customer_id=c.id WHERE s.id=sid AND s.enabled AND c.active AND s.verified_at IS NOT NULL
 AND a.status='approved' AND a.evidence<>'' AND a.approved_at IS NOT NULL AND a.approved_by<>''
 AND a.contacts<>'[]'::jsonb AND a.authorized_users<>'[]'::jsonb AND a.billing_fields<>'[]'::jsonb AND a.storage_region<>'' AND a.retention_days>0
 AND a.expected_accounts ? s.account_id AND a.optional_capabilities @> s.approved_capabilities
 AND s.trust_checks @> jsonb_build_object('correct_external_id','passed','missing_external_id','denied','wrong_external_id','denied','account_identity','passed','exact_collector_principal','passed','connection_version',s.connection_version)
 AND EXISTS(SELECT 1 FROM billing_roleapproval r WHERE r.source_id=s.id AND r.role_arn=s.role_arn AND r.connection_version=s.connection_version AND r.status='approved' AND r.approved_at IS NOT NULL AND r.evidence<>''))
$$;
REVOKE ALL ON FUNCTION billing_customer_configuration(uuid),billing_connection_ready(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing_customer_configuration(uuid),billing_connection_ready(uuid) TO billing_web,billing_collector;
