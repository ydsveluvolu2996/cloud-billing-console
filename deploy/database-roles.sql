-- Apply after migrations as the administration/schema owner, never a runtime role.
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
CREATE OR REPLACE FUNCTION billing_parent_customer(tab text,rid text) RETURNS uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$ BEGIN CASE tab
 WHEN 'billing_customer' THEN RETURN (SELECT t0.id FROM billing_customer t0  WHERE t0.id::text=rid);
 WHEN 'billing_billingsource' THEN RETURN (SELECT t0.customer_id FROM billing_billingsource t0  WHERE t0.id::text=rid);
 WHEN 'billing_cost' THEN RETURN (SELECT t0.customer_id FROM billing_cost t0  WHERE t0.id::text=rid);
 WHEN 'billing_accountassignment' THEN RETURN (SELECT t0.customer_id FROM billing_accountassignment t0  WHERE t0.id::text=rid);
 WHEN 'billing_project' THEN RETURN (SELECT t0.customer_id FROM billing_project t0  WHERE t0.id::text=rid);
 WHEN 'billing_allocationrule' THEN RETURN (SELECT t1.customer_id FROM billing_allocationrule t0 JOIN billing_project t1 ON t0.project_id=t1.id WHERE t0.id::text=rid);
 WHEN 'billing_projectcost' THEN RETURN (SELECT t1.customer_id FROM billing_projectcost t0 JOIN billing_project t1 ON t0.project_id=t1.id WHERE t0.id::text=rid);
 WHEN 'billing_budget' THEN RETURN (SELECT t0.customer_id FROM billing_budget t0  WHERE t0.id::text=rid);
 WHEN 'billing_budgetamount' THEN RETURN (SELECT t1.customer_id FROM billing_budgetamount t0 JOIN billing_budget t1 ON t0.budget_id=t1.id WHERE t0.id::text=rid);
 WHEN 'billing_budgetevaluation' THEN RETURN (SELECT t1.customer_id FROM billing_budgetevaluation t0 JOIN billing_budget t1 ON t0.budget_id=t1.id WHERE t0.id::text=rid);
 WHEN 'billing_alert' THEN RETURN (SELECT t1.customer_id FROM billing_alert t0 JOIN billing_budget t1 ON t0.budget_id=t1.id WHERE t0.id::text=rid);
 WHEN 'billing_alliancerevision' THEN RETURN (SELECT t1.customer_id FROM billing_alliancerevision t0 JOIN billing_alliancerecord t1 ON t0.record_id=t1.id WHERE t0.id::text=rid);
 WHEN 'billing_alliancerecord' THEN RETURN (SELECT t0.customer_id FROM billing_alliancerecord t0  WHERE t0.id::text=rid);
 WHEN 'billing_allianceservicenote' THEN RETURN (SELECT t1.customer_id FROM billing_allianceservicenote t0 JOIN billing_alliancerecord t1 ON t0.record_id=t1.id WHERE t0.id::text=rid);
 WHEN 'billing_customerapproval' THEN RETURN (SELECT t0.customer_id FROM billing_customerapproval t0  WHERE t0.id::text=rid);
 WHEN 'billing_rolloutreadiness' THEN RETURN (SELECT t0.customer_id FROM billing_rolloutreadiness t0  WHERE t0.id::text=rid);
 WHEN 'billing_reconciliationrun' THEN RETURN (SELECT t0.customer_id FROM billing_reconciliationrun t0  WHERE t0.id::text=rid);
 WHEN 'billing_offboardingrecord' THEN RETURN (SELECT t0.customer_id FROM billing_offboardingrecord t0  WHERE t0.id::text=rid);
 WHEN 'billing_operationalalert' THEN RETURN (SELECT t0.customer_id FROM billing_operationalalert t0  WHERE t0.id::text=rid);
 WHEN 'billing_alertroute' THEN RETURN (SELECT t0.customer_id FROM billing_alertroute t0  WHERE t0.id::text=rid);
 WHEN 'billing_portalinvitation' THEN RETURN (SELECT t0.customer_id FROM billing_portalinvitation t0  WHERE t0.id::text=rid);
 WHEN 'billing_savedreport' THEN RETURN (SELECT t0.customer_id FROM billing_savedreport t0  WHERE t0.id::text=rid);
 WHEN 'billing_auditevent' THEN RETURN (SELECT t0.customer_id FROM billing_auditevent t0  WHERE t0.id::text=rid);
 WHEN 'billing_syncrun' THEN RETURN (SELECT t0.customer_id FROM billing_syncrun t0  WHERE t0.id::text=rid);
 WHEN 'billing_explorerquery' THEN RETURN (SELECT t0.customer_id FROM billing_explorerquery t0  WHERE t0.id::text=rid);
 WHEN 'billing_roleapproval' THEN RETURN (SELECT t1.customer_id FROM billing_roleapproval t0 JOIN billing_billingsource t1 ON t0.source_id=t1.id WHERE t0.id::text=rid);
 WHEN 'billing_collectionperiod' THEN RETURN (SELECT t1.customer_id FROM billing_collectionperiod t0 JOIN billing_billingsource t1 ON t0.source_id=t1.id WHERE t0.id::text=rid);
 WHEN 'billing_job' THEN RETURN (SELECT t1.customer_id FROM billing_job t0 JOIN billing_billingsource t1 ON t0.source_id=t1.id WHERE t0.id::text=rid);
 WHEN 'billing_importedbudget' THEN RETURN (SELECT t1.customer_id FROM billing_importedbudget t0 JOIN billing_billingsource t1 ON t0.source_id=t1.id WHERE t0.id::text=rid);
 ELSE RETURN NULL; END CASE; END $$;
REVOKE ALL ON FUNCTION billing_parent_customer(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing_parent_customer(text,text) TO billing_web;
CREATE OR REPLACE FUNCTION billing_account_number(rid bigint) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$ SELECT account_id FROM billing_awsaccount WHERE id=rid $$;
REVOKE ALL ON FUNCTION billing_account_number(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing_account_number(bigint) TO billing_web;
CREATE OR REPLACE FUNCTION billing_account_access(aid text,writing boolean) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT EXISTS (SELECT 1 FROM billing_accountassignment a JOIN billing_awsaccount x ON x.id=a.account_id WHERE x.account_id=aid AND billing_can_access(a.customer_id,aid,writing))
$$;
REVOKE ALL ON FUNCTION billing_account_access(text,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing_account_access(text,boolean) TO billing_web;

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
ALTER TABLE billing_customer ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_customer;
CREATE POLICY web_scope ON billing_customer TO billing_web USING (CASE WHEN (SELECT billing_can_access(NULL,NULL,false)) THEN true ELSE (billing_customer_visible(id)) END) WITH CHECK (billing_can_access(id,NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_customer;
CREATE POLICY collector_scope ON billing_customer TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_customer TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_customer TO billing_web;
GRANT SELECT ON billing_customer TO billing_collector;
ALTER TABLE billing_billingsource ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_billingsource;
CREATE POLICY web_scope ON billing_billingsource TO billing_web USING (billing_source_visible(id)) WITH CHECK (billing_can_access(customer_id,NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_billingsource;
CREATE POLICY collector_scope ON billing_billingsource TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_billingsource TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_billingsource TO billing_web;
GRANT SELECT ON billing_billingsource TO billing_collector;
ALTER TABLE billing_awsaccount ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_awsaccount;
CREATE POLICY web_scope ON billing_awsaccount TO billing_web USING (billing_account_access(account_id,false) OR billing_can_access(billing_parent_customer('billing_billingsource',source_id::text),NULL,false)) WITH CHECK (billing_account_access(account_id,true) OR billing_can_access(billing_parent_customer('billing_billingsource',source_id::text),NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_awsaccount;
CREATE POLICY collector_scope ON billing_awsaccount TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_awsaccount TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_awsaccount TO billing_web;
GRANT SELECT ON billing_awsaccount TO billing_collector;
GRANT INSERT,UPDATE,DELETE ON billing_awsaccount TO billing_collector;
ALTER TABLE billing_accountassignment ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_accountassignment;
CREATE POLICY web_scope ON billing_accountassignment TO billing_web USING (billing_can_access(customer_id,billing_account_number(account_id),false)) WITH CHECK (billing_can_access(customer_id,billing_account_number(account_id),true));
DROP POLICY IF EXISTS collector_scope ON billing_accountassignment;
CREATE POLICY collector_scope ON billing_accountassignment TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_accountassignment TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_accountassignment TO billing_web;
GRANT SELECT ON billing_accountassignment TO billing_collector;
GRANT INSERT,UPDATE,DELETE ON billing_accountassignment TO billing_collector;
ALTER TABLE billing_cost ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_cost;
CREATE POLICY web_scope ON billing_cost TO billing_web USING (CASE WHEN (SELECT billing_can_access(NULL,NULL,false)) THEN true ELSE (billing_can_access(customer_id,account_id,false)) END) WITH CHECK (billing_can_access(customer_id,account_id,true));
DROP POLICY IF EXISTS collector_scope ON billing_cost;
CREATE POLICY collector_scope ON billing_cost TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_cost TO billing_web;
GRANT SELECT ON billing_cost TO billing_collector;
GRANT INSERT,UPDATE,DELETE ON billing_cost TO billing_collector;
ALTER TABLE billing_collectionperiod ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_collectionperiod;
CREATE POLICY web_scope ON billing_collectionperiod TO billing_web USING (billing_can_access(billing_parent_customer('billing_billingsource',source_id::text),NULL,false)) WITH CHECK (billing_can_access(billing_parent_customer('billing_billingsource',source_id::text),NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_collectionperiod;
CREATE POLICY collector_scope ON billing_collectionperiod TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_collectionperiod TO billing_web;
GRANT SELECT ON billing_collectionperiod TO billing_collector;
GRANT INSERT,UPDATE,DELETE ON billing_collectionperiod TO billing_collector;
ALTER TABLE billing_syncrun ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_syncrun;
CREATE POLICY web_scope ON billing_syncrun TO billing_web USING (billing_can_access(customer_id,NULL,false)) WITH CHECK (billing_can_access(customer_id,NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_syncrun;
CREATE POLICY collector_scope ON billing_syncrun TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_syncrun TO billing_web;
GRANT SELECT ON billing_syncrun TO billing_collector;
GRANT INSERT,UPDATE,DELETE ON billing_syncrun TO billing_collector;
ALTER TABLE billing_project ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_project;
CREATE POLICY web_scope ON billing_project TO billing_web USING (billing_can_access(customer_id,NULL,false)) WITH CHECK (billing_can_access(customer_id,NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_project;
CREATE POLICY collector_scope ON billing_project TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_project TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_project TO billing_web;
GRANT SELECT ON billing_project TO billing_collector;
ALTER TABLE billing_allocationrule ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_allocationrule;
CREATE POLICY web_scope ON billing_allocationrule TO billing_web USING (billing_can_access(billing_parent_customer('billing_project',project_id::text),NULL,false)) WITH CHECK (billing_can_access(billing_parent_customer('billing_project',project_id::text),NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_allocationrule;
CREATE POLICY collector_scope ON billing_allocationrule TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_allocationrule TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_allocationrule TO billing_web;
GRANT SELECT ON billing_allocationrule TO billing_collector;
ALTER TABLE billing_projectcost ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_projectcost;
CREATE POLICY web_scope ON billing_projectcost TO billing_web USING (billing_can_access(billing_parent_customer('billing_project',project_id::text),account_id,false)) WITH CHECK (billing_can_access(billing_parent_customer('billing_project',project_id::text),account_id,true));
DROP POLICY IF EXISTS collector_scope ON billing_projectcost;
CREATE POLICY collector_scope ON billing_projectcost TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_projectcost TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_projectcost TO billing_web;
GRANT SELECT ON billing_projectcost TO billing_collector;
GRANT INSERT,UPDATE,DELETE ON billing_projectcost TO billing_collector;
ALTER TABLE billing_budget ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_budget;
CREATE POLICY web_scope ON billing_budget TO billing_web USING (billing_can_access(customer_id,NULL,false)) WITH CHECK (billing_can_access(customer_id,NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_budget;
CREATE POLICY collector_scope ON billing_budget TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_budget TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_budget TO billing_web;
GRANT SELECT ON billing_budget TO billing_collector;
ALTER TABLE billing_budgetamount ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_budgetamount;
CREATE POLICY web_scope ON billing_budgetamount TO billing_web USING (billing_can_access(billing_parent_customer('billing_budget',budget_id::text),NULL,false)) WITH CHECK (billing_can_access(billing_parent_customer('billing_budget',budget_id::text),NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_budgetamount;
CREATE POLICY collector_scope ON billing_budgetamount TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_budgetamount TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_budgetamount TO billing_web;
GRANT SELECT ON billing_budgetamount TO billing_collector;
ALTER TABLE billing_budgetevaluation ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_budgetevaluation;
CREATE POLICY web_scope ON billing_budgetevaluation TO billing_web USING (billing_can_access(billing_parent_customer('billing_budget',budget_id::text),NULL,false)) WITH CHECK (billing_can_access(billing_parent_customer('billing_budget',budget_id::text),NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_budgetevaluation;
CREATE POLICY collector_scope ON billing_budgetevaluation TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_budgetevaluation TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_budgetevaluation TO billing_web;
GRANT SELECT ON billing_budgetevaluation TO billing_collector;
GRANT INSERT,UPDATE,DELETE ON billing_budgetevaluation TO billing_collector;
ALTER TABLE billing_importedbudget ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_importedbudget;
CREATE POLICY web_scope ON billing_importedbudget TO billing_web USING (billing_can_access(billing_parent_customer('billing_billingsource',source_id::text),owning_account_id,false)) WITH CHECK (billing_can_access(billing_parent_customer('billing_billingsource',source_id::text),owning_account_id,true));
DROP POLICY IF EXISTS collector_scope ON billing_importedbudget;
CREATE POLICY collector_scope ON billing_importedbudget TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_importedbudget TO billing_web;
GRANT SELECT ON billing_importedbudget TO billing_collector;
GRANT INSERT,UPDATE,DELETE ON billing_importedbudget TO billing_collector;
ALTER TABLE billing_alert ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_alert;
CREATE POLICY web_scope ON billing_alert TO billing_web USING (billing_can_access(billing_parent_customer('billing_budget',budget_id::text),NULL,false)) WITH CHECK (billing_can_access(billing_parent_customer('billing_budget',budget_id::text),NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_alert;
CREATE POLICY collector_scope ON billing_alert TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_alert TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_alert TO billing_web;
GRANT SELECT ON billing_alert TO billing_collector;
GRANT INSERT,UPDATE,DELETE ON billing_alert TO billing_collector;
ALTER TABLE billing_job ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_job;
CREATE POLICY web_scope ON billing_job TO billing_web USING ((billing_can_access(billing_parent_customer('billing_billingsource',source_id::text),NULL,false)) OR (kind='explorer_refresh' AND EXISTS(SELECT 1 FROM billing_explorerquery q WHERE q.source_id=billing_job.source_id AND q.requested_by_id::text=current_setting('billing.user_id',true)))) WITH CHECK ((billing_can_access(billing_parent_customer('billing_billingsource',source_id::text),NULL,true)) OR (kind='explorer_refresh' AND EXISTS(SELECT 1 FROM billing_explorerquery q WHERE q.source_id=billing_job.source_id AND q.requested_by_id::text=current_setting('billing.user_id',true))));
DROP POLICY IF EXISTS collector_scope ON billing_job;
CREATE POLICY collector_scope ON billing_job TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_job TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_job TO billing_web;
GRANT SELECT ON billing_job TO billing_collector;
GRANT INSERT,UPDATE,DELETE ON billing_job TO billing_collector;
ALTER TABLE billing_auditevent ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_auditevent;
CREATE POLICY web_scope ON billing_auditevent TO billing_web USING (billing_can_access(customer_id,NULL,false)) WITH CHECK (actor=(SELECT username FROM auth_user WHERE id::text=current_setting('billing.user_id',true)) OR current_setting('billing.user_id',true) IS NULL OR current_setting('billing.user_id',true)='');
DROP POLICY IF EXISTS collector_scope ON billing_auditevent;
CREATE POLICY collector_scope ON billing_auditevent TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_auditevent TO billing_web;
GRANT INSERT ON billing_auditevent TO billing_web,billing_collector;
GRANT SELECT ON billing_auditevent TO billing_collector;
ALTER TABLE billing_explorerquery ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_explorerquery;
CREATE POLICY web_scope ON billing_explorerquery TO billing_web USING (billing_customer_visible(customer_id) AND requested_by_id::text=current_setting('billing.user_id',true) AND scope_fingerprint<>'' AND scope_fingerprint=current_setting('billing.scope_fingerprint',true)) WITH CHECK (billing_customer_visible(customer_id) AND requested_by_id::text=current_setting('billing.user_id',true) AND scope_fingerprint<>'' AND scope_fingerprint=current_setting('billing.scope_fingerprint',true));
DROP POLICY IF EXISTS collector_scope ON billing_explorerquery;
CREATE POLICY collector_scope ON billing_explorerquery TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_explorerquery TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_explorerquery TO billing_web;
GRANT SELECT ON billing_explorerquery TO billing_collector;
GRANT INSERT,UPDATE,DELETE ON billing_explorerquery TO billing_collector;
ALTER TABLE billing_savedreport ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_savedreport;
CREATE POLICY web_scope ON billing_savedreport TO billing_web USING (billing_customer_visible(customer_id) AND created_by=(SELECT username FROM auth_user WHERE id::text=current_setting('billing.user_id',true))) WITH CHECK (billing_customer_visible(customer_id) AND created_by=(SELECT username FROM auth_user WHERE id::text=current_setting('billing.user_id',true)));
DROP POLICY IF EXISTS collector_scope ON billing_savedreport;
CREATE POLICY collector_scope ON billing_savedreport TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_savedreport TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_savedreport TO billing_web;
GRANT SELECT ON billing_savedreport TO billing_collector;
ALTER TABLE billing_bulkimport ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_bulkimport;
CREATE POLICY web_scope ON billing_bulkimport TO billing_web USING (requested_by_id::text=current_setting('billing.user_id',true) AND scope_fingerprint<>'' AND scope_fingerprint=current_setting('billing.scope_fingerprint',true)) WITH CHECK (requested_by_id::text=current_setting('billing.user_id',true) AND scope_fingerprint<>'' AND scope_fingerprint=current_setting('billing.scope_fingerprint',true));
DROP POLICY IF EXISTS collector_scope ON billing_bulkimport;
CREATE POLICY collector_scope ON billing_bulkimport TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_bulkimport TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_bulkimport TO billing_web;
ALTER TABLE billing_alliancerecord ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_alliancerecord;
CREATE POLICY web_scope ON billing_alliancerecord TO billing_web USING (billing_can_access(customer_id,billing_account_number(account_id),false)) WITH CHECK (billing_can_access(customer_id,billing_account_number(account_id),true));
DROP POLICY IF EXISTS collector_scope ON billing_alliancerecord;
CREATE POLICY collector_scope ON billing_alliancerecord TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_alliancerecord TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_alliancerecord TO billing_web;
GRANT SELECT ON billing_alliancerecord TO billing_collector;
ALTER TABLE billing_allianceservicenote ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_allianceservicenote;
CREATE POLICY web_scope ON billing_allianceservicenote TO billing_web USING (billing_can_access(billing_parent_customer('billing_alliancerecord',record_id::text),(SELECT billing_account_number(account_id) FROM billing_alliancerecord WHERE id=record_id),false)) WITH CHECK (billing_can_access(billing_parent_customer('billing_alliancerecord',record_id::text),(SELECT billing_account_number(account_id) FROM billing_alliancerecord WHERE id=record_id),true));
DROP POLICY IF EXISTS collector_scope ON billing_allianceservicenote;
CREATE POLICY collector_scope ON billing_allianceservicenote TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_allianceservicenote TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_allianceservicenote TO billing_web;
GRANT SELECT ON billing_allianceservicenote TO billing_collector;
ALTER TABLE billing_customerapproval ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_customerapproval;
CREATE POLICY web_scope ON billing_customerapproval TO billing_web USING (billing_can_access(customer_id,NULL,false)) WITH CHECK (billing_can_access(customer_id,NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_customerapproval;
CREATE POLICY collector_scope ON billing_customerapproval TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_customerapproval TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_customerapproval TO billing_web;
GRANT SELECT ON billing_customerapproval TO billing_collector;
ALTER TABLE billing_roleapproval ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_roleapproval;
CREATE POLICY web_scope ON billing_roleapproval TO billing_web USING (billing_can_access(billing_parent_customer('billing_billingsource',source_id::text),NULL,false)) WITH CHECK (billing_can_access(billing_parent_customer('billing_billingsource',source_id::text),NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_roleapproval;
CREATE POLICY collector_scope ON billing_roleapproval TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_roleapproval TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_roleapproval TO billing_web;
GRANT SELECT ON billing_roleapproval TO billing_collector;
ALTER TABLE billing_rolloutreadiness ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_rolloutreadiness;
CREATE POLICY web_scope ON billing_rolloutreadiness TO billing_web USING (billing_can_access(customer_id,NULL,false)) WITH CHECK (billing_can_access(customer_id,NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_rolloutreadiness;
CREATE POLICY collector_scope ON billing_rolloutreadiness TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_rolloutreadiness TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_rolloutreadiness TO billing_web;
GRANT SELECT ON billing_rolloutreadiness TO billing_collector;
ALTER TABLE billing_reconciliationrun ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_reconciliationrun;
CREATE POLICY web_scope ON billing_reconciliationrun TO billing_web USING (billing_can_access(customer_id,NULL,false)) WITH CHECK (billing_can_access(customer_id,NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_reconciliationrun;
CREATE POLICY collector_scope ON billing_reconciliationrun TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_reconciliationrun TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_reconciliationrun TO billing_web;
GRANT SELECT ON billing_reconciliationrun TO billing_collector;
ALTER TABLE billing_offboardingrecord ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_offboardingrecord;
CREATE POLICY web_scope ON billing_offboardingrecord TO billing_web USING (billing_can_access(customer_id,NULL,false)) WITH CHECK (billing_can_access(customer_id,NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_offboardingrecord;
CREATE POLICY collector_scope ON billing_offboardingrecord TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_offboardingrecord TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_offboardingrecord TO billing_web;
GRANT SELECT ON billing_offboardingrecord TO billing_collector;
ALTER TABLE billing_operationalalert ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_operationalalert;
CREATE POLICY web_scope ON billing_operationalalert TO billing_web USING (billing_can_access(customer_id,NULL,false)) WITH CHECK (billing_can_access(customer_id,NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_operationalalert;
CREATE POLICY collector_scope ON billing_operationalalert TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_operationalalert TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_operationalalert TO billing_web;
GRANT SELECT ON billing_operationalalert TO billing_collector;
GRANT INSERT,UPDATE,DELETE ON billing_operationalalert TO billing_collector;
ALTER TABLE billing_alertroute ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_alertroute;
CREATE POLICY web_scope ON billing_alertroute TO billing_web USING (billing_can_access(customer_id,NULL,false)) WITH CHECK (billing_can_access(customer_id,NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_alertroute;
CREATE POLICY collector_scope ON billing_alertroute TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_alertroute TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_alertroute TO billing_web;
GRANT SELECT ON billing_alertroute TO billing_collector;
ALTER TABLE billing_portalinvitation ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_portalinvitation;
CREATE POLICY web_scope ON billing_portalinvitation TO billing_web USING (billing_can_access(customer_id,NULL,false)) WITH CHECK (billing_can_access(customer_id,NULL,true));
DROP POLICY IF EXISTS collector_scope ON billing_portalinvitation;
CREATE POLICY collector_scope ON billing_portalinvitation TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_portalinvitation TO billing_web;
GRANT INSERT,UPDATE,DELETE ON billing_portalinvitation TO billing_web;
ALTER TABLE billing_alliancerevision ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_scope ON billing_alliancerevision;
CREATE POLICY web_scope ON billing_alliancerevision TO billing_web USING (billing_can_access(billing_parent_customer('billing_alliancerecord',record_id::text),(SELECT billing_account_number(account_id) FROM billing_alliancerecord WHERE id=record_id),false)) WITH CHECK (billing_can_access(billing_parent_customer('billing_alliancerecord',record_id::text),(SELECT billing_account_number(account_id) FROM billing_alliancerecord WHERE id=record_id),true));
DROP POLICY IF EXISTS collector_scope ON billing_alliancerevision;
CREATE POLICY collector_scope ON billing_alliancerevision TO billing_collector USING (true) WITH CHECK (true);
GRANT SELECT ON billing_alliancerevision TO billing_web;
GRANT INSERT ON billing_alliancerevision TO billing_web;
GRANT SELECT ON billing_alliancerevision TO billing_collector;
GRANT SELECT ON billing_customermembership,billing_usersecurity,auth_user TO billing_web;
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
CREATE OR REPLACE FUNCTION billing_guard_profile() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF current_user='billing_web' AND (NEW.portfolio_access OR NEW.oidc_subject<>'' OR NEW.oidc_issuer<>'') THEN RAISE EXCEPTION 'Administrative profile grants require the admin identity'; END IF;
 RETURN NEW; END $$;
DROP TRIGGER IF EXISTS guard_profile ON billing_usersecurity;
CREATE TRIGGER guard_profile BEFORE INSERT ON billing_usersecurity FOR EACH ROW EXECUTE FUNCTION billing_guard_profile();

CREATE OR REPLACE FUNCTION billing_guard_approval() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF current_user='billing_web' AND (NEW.status NOT IN ('pending','requested') OR NEW.approved_by<>'' OR NEW.approved_at IS NOT NULL) THEN RAISE EXCEPTION 'Approval requires administration identity'; END IF;
 RETURN NEW; END $$;
DROP TRIGGER IF EXISTS guard_customer_approval ON billing_customerapproval;
CREATE TRIGGER guard_customer_approval BEFORE INSERT OR UPDATE ON billing_customerapproval FOR EACH ROW EXECUTE FUNCTION billing_guard_approval();
DROP TRIGGER IF EXISTS guard_role_approval ON billing_roleapproval;
CREATE TRIGGER guard_role_approval BEFORE INSERT OR UPDATE ON billing_roleapproval FOR EACH ROW EXECUTE FUNCTION billing_guard_approval();
-- Narrow effective-ownership restamp; the web role cannot update cost amounts.
CREATE OR REPLACE FUNCTION billing_restamp_ownership(aid text, first_day date) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE changed bigint;
BEGIN
 PERFORM 1 FROM billing_awsaccount WHERE account_id=aid FOR UPDATE;
 IF NOT EXISTS(SELECT 1 FROM billing_accountassignment a JOIN billing_awsaccount x ON x.id=a.account_id WHERE x.account_id=aid AND billing_can_access(a.customer_id,aid,true))
 OR EXISTS(SELECT 1 FROM billing_cost WHERE account_id=aid AND day>=first_day AND NOT billing_can_access(customer_id,aid,true))
 OR EXISTS(SELECT 1 FROM billing_accountassignment a JOIN billing_awsaccount x ON x.id=a.account_id WHERE x.account_id=aid AND (a."end" IS NULL OR a."end">first_day) AND NOT billing_can_access(a.customer_id,aid,true))
 THEN RAISE EXCEPTION 'Ownership transfer requires authorized old and new customer scopes'; END IF;
 UPDATE billing_cost c SET customer_id=(SELECT a.customer_id FROM billing_accountassignment a JOIN billing_awsaccount x ON x.id=a.account_id WHERE x.account_id=aid AND a.start<=c.day AND (a."end" IS NULL OR a."end">c.day))
 WHERE c.account_id=aid AND c.day>=first_day;
 GET DIAGNOSTICS changed=ROW_COUNT;
 RETURN changed;
END $$;
REVOKE ALL ON FUNCTION billing_restamp_ownership(text,date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing_restamp_ownership(text,date) TO billing_web;
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
-- Narrow capability admission; no general membership/profile write grant to the web login.
CREATE OR REPLACE FUNCTION billing_accept_invitation(digest text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE invitation billing_portalinvitation; uid integer; cid uuid;
BEGIN
 IF current_setting('billing.external_enabled',true) IS DISTINCT FROM 'true' THEN RAISE EXCEPTION 'Portal disabled'; END IF;
 SELECT id INTO uid FROM auth_user WHERE id::text=current_setting('billing.user_id',true) AND is_active;
 SELECT i.* INTO invitation FROM billing_portalinvitation i JOIN auth_user u ON u.id=uid
 WHERE i.token_hash=digest AND i.target_user_id=uid AND lower(i.email)=lower(u.email) AND i.accepted_at IS NULL AND i.revoked_at IS NULL AND i.expires_at>now() FOR UPDATE OF i;
 IF invitation.id IS NULL THEN RAISE EXCEPTION 'Invalid invitation'; END IF;
 cid:=invitation.customer_id;
 IF NOT EXISTS(SELECT 1 FROM billing_customer c JOIN billing_customerapproval a ON a.customer_id=c.id
 JOIN billing_rolloutreadiness r ON r.customer_id=c.id WHERE c.id=cid AND c.active AND a.status='approved' AND a.evidence<>''
 AND r.owner<>'' AND r.planned_date IS NOT NULL AND r.security_evidence<>'' AND r.independent_review<>''
 AND COALESCE(r.checklist->>'rollback_readiness','') NOT IN ('','false') AND COALESCE(r.checklist->>'rollback_evidence','')<>'')
 OR NOT EXISTS(SELECT 1 FROM billing_reconciliationrun WHERE customer_id=cid AND source_id IS NULL AND NOT synthetic AND result->>'passed'='true' AND result->'configuration'=billing_customer_configuration(cid))
 OR NOT EXISTS(SELECT 1 FROM billing_customermembership WHERE customer_id=cid AND active AND (expires_at IS NULL OR expires_at>now()))
 OR NOT EXISTS(SELECT 1 FROM billing_accountassignment WHERE customer_id=cid AND "end" IS NULL)
 OR billing_customer_configuration(cid)='{}'::jsonb
 OR EXISTS(SELECT 1 FROM jsonb_object_keys(billing_customer_configuration(cid)) sid WHERE NOT billing_connection_ready(sid::uuid) OR NOT EXISTS(SELECT 1 FROM billing_billingsource WHERE id=sid::uuid AND discovered_at IS NOT NULL))
 THEN RAISE EXCEPTION 'Readiness gates incomplete'; END IF;
 IF EXISTS(SELECT 1 FROM jsonb_array_elements_text(invitation.account_ids) x WHERE NOT EXISTS(
 SELECT 1 FROM billing_accountassignment a JOIN billing_awsaccount b ON b.id=a.account_id WHERE a.customer_id=cid AND a."end" IS NULL AND b.account_id=x))
 THEN RAISE EXCEPTION 'Invitation account assignment changed'; END IF;
 IF EXISTS(SELECT 1 FROM billing_customermembership WHERE user_id=uid AND role<>'customer' AND active)
 OR EXISTS(SELECT 1 FROM billing_usersecurity WHERE user_id=uid AND portfolio_access)
 THEN RAISE EXCEPTION 'Use a separate external identity'; END IF;
 INSERT INTO billing_customermembership(user_id,customer_id,role,account_ids,active,expires_at,support_reason,created_at)
 VALUES(uid,cid,'customer',invitation.account_ids,true,NULL,'',now()) ON CONFLICT(user_id,customer_id)
 DO UPDATE SET role='customer',account_ids=EXCLUDED.account_ids,active=true,expires_at=NULL,support_reason='';
 UPDATE billing_usersecurity SET external=true,session_version=session_version+1 WHERE user_id=uid;
 UPDATE billing_portalinvitation SET accepted_at=now() WHERE id=invitation.id;
 RETURN cid;
END $$;
REVOKE ALL ON FUNCTION billing_accept_invitation(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing_accept_invitation(text) TO billing_web;
-- The web role gets only this guarded capability, not general identity write grants.
CREATE OR REPLACE FUNCTION billing_admin_user(command jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE actor_id integer; actor_name text; target_id integer; target_name text;
 action text:=command->>'action'; role_name text:=command->>'role'; enabled boolean;
 grant_row jsonb; cid uuid; accounts jsonb; event_id bigint; details jsonb; event_time timestamptz:=now();
BEGIN
 -- Serialize administrator changes so two administrators cannot remove each other.
 PERFORM pg_advisory_xact_lock(582287676,741);
 SELECT u.id,u.username INTO actor_id,actor_name FROM auth_user u JOIN billing_usersecurity s ON s.user_id=u.id
 WHERE u.id::text=current_setting('billing.user_id',true) AND u.is_active AND u.is_superuser
 AND s.portfolio_access AND NOT s.external FOR UPDATE OF u;
 IF actor_id IS NULL OR current_setting('billing.mfa_verified',true) IS DISTINCT FROM 'true' THEN
  RAISE EXCEPTION 'MFA-verified portfolio administrator required' USING ERRCODE='42501';
 END IF;
 IF action NOT IN ('create','update','delete') OR action IS NULL THEN RAISE EXCEPTION 'Invalid user action'; END IF;
 target_id:=NULLIF(command->>'id','')::integer;
 IF action='create' THEN
  IF target_id IS NOT NULL THEN RAISE EXCEPTION 'New users cannot supply an ID'; END IF;
 ELSE
  SELECT username INTO target_name FROM auth_user WHERE id=target_id FOR UPDATE;
  IF target_name IS NULL THEN RAISE EXCEPTION 'User not found'; END IF;
 END IF;
 IF action='delete' THEN
  IF target_id=actor_id THEN RAISE EXCEPTION 'You cannot delete your own account'; END IF;
  IF EXISTS(SELECT 1 FROM django_admin_log WHERE user_id=target_id) THEN
   RAISE EXCEPTION 'Disable this user to preserve historical administration records';
  END IF;
  UPDATE billing_explorerquery SET requested_by_id=NULL WHERE requested_by_id=target_id;
  UPDATE billing_bulkimport SET requested_by_id=NULL WHERE requested_by_id=target_id;
  UPDATE billing_portalinvitation SET target_user_id=NULL,revoked_at=COALESCE(revoked_at,now()) WHERE target_user_id=target_id;
  DELETE FROM billing_customermembership WHERE user_id=target_id;
  DELETE FROM otp_totp_totpdevice WHERE user_id=target_id;
  DELETE FROM billing_usersecurity WHERE user_id=target_id;
  DELETE FROM auth_user_groups WHERE user_id=target_id;
  DELETE FROM auth_user_user_permissions WHERE user_id=target_id;
  DELETE FROM auth_user WHERE id=target_id;
 ELSE
  enabled:=(command->>'active')::boolean;
  IF enabled IS NULL OR role_name IS NULL OR role_name NOT IN ('admin','operator','viewer') THEN RAISE EXCEPTION 'Invalid user role or status'; END IF;
  IF target_id=actor_id AND (NOT enabled OR role_name<>'admin') THEN RAISE EXCEPTION 'You cannot remove your own administrator access'; END IF;
  IF jsonb_typeof(command->'grants') IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'Customer grants must be an array'; END IF;
  IF role_name='admin' AND command->'grants'<>'[]'::jsonb THEN RAISE EXCEPTION 'Administrators already have all customer access'; END IF;
  FOR grant_row IN SELECT value FROM jsonb_array_elements(command->'grants') LOOP
   cid:=(grant_row->>'customer_id')::uuid; accounts:=grant_row->'account_ids';
   IF NOT EXISTS(SELECT 1 FROM billing_customer WHERE id=cid AND active) OR (grant_row->>'role') IS DISTINCT FROM role_name
    OR jsonb_typeof(accounts) IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'Invalid customer grant'; END IF;
   IF EXISTS(SELECT 1 FROM jsonb_array_elements_text(accounts) a WHERE a !~ '^[0-9]{12}$' OR NOT EXISTS(
    SELECT 1 FROM billing_accountassignment x JOIN billing_awsaccount y ON y.id=x.account_id WHERE x.customer_id=cid AND x."end" IS NULL AND y.account_id=a))
    THEN RAISE EXCEPTION 'Account is not assigned to the selected customer'; END IF;
  END LOOP;
  IF action='create' THEN
   target_name:=command->>'username';
   IF COALESCE(length(target_name),0) NOT BETWEEN 1 AND 150 OR EXISTS(SELECT 1 FROM auth_user WHERE lower(username)=lower(target_name)) THEN RAISE EXCEPTION 'Username is unavailable'; END IF;
   IF COALESCE(command->>'password','') NOT LIKE 'pbkdf2_sha256$%' THEN RAISE EXCEPTION 'A validated password hash is required'; END IF;
   INSERT INTO auth_user(username,password,email,first_name,last_name,is_staff,is_active,is_superuser,date_joined)
   VALUES(target_name,command->>'password',COALESCE(command->>'email',''),COALESCE(command->>'first_name',''),COALESCE(command->>'last_name',''),role_name='admin',enabled,role_name='admin',now()) RETURNING id INTO target_id;
   INSERT INTO billing_usersecurity(user_id,portfolio_access,external,session_version,oidc_subject,oidc_issuer,recovery_hashes,recovery_failed,recovery_locked_until)
   VALUES(target_id,role_name='admin',false,1,'','','[]',0,NULL);
  ELSE
   IF COALESCE(command->>'password','')<>'' AND command->>'password' NOT LIKE 'pbkdf2_sha256$%' THEN RAISE EXCEPTION 'A validated password hash is required'; END IF;
   UPDATE auth_user SET email=COALESCE(command->>'email',''),first_name=COALESCE(command->>'first_name',''),last_name=COALESCE(command->>'last_name',''),
    is_staff=role_name='admin',is_superuser=role_name='admin',is_active=enabled,
    password=CASE WHEN COALESCE(command->>'password','')='' THEN password ELSE command->>'password' END WHERE id=target_id;
   INSERT INTO billing_usersecurity(user_id,portfolio_access,external,session_version,oidc_subject,oidc_issuer,recovery_hashes,recovery_failed,recovery_locked_until)
   VALUES(target_id,role_name='admin',false,1,'','','[]',0,NULL) ON CONFLICT(user_id)
   DO UPDATE SET portfolio_access=EXCLUDED.portfolio_access,external=false,session_version=billing_usersecurity.session_version+1;
  END IF;
  UPDATE billing_customermembership SET active=false WHERE user_id=target_id;
  FOR grant_row IN SELECT value FROM jsonb_array_elements(command->'grants') LOOP
   INSERT INTO billing_customermembership(user_id,customer_id,role,account_ids,active,created_at,expires_at,support_reason)
   VALUES(target_id,(grant_row->>'customer_id')::uuid,role_name,grant_row->'account_ids',true,now(),NULL,'') ON CONFLICT(user_id,customer_id)
   DO UPDATE SET role=EXCLUDED.role,account_ids=EXCLUDED.account_ids,active=true,expires_at=NULL,support_reason='';
  END LOOP;
 END IF;
 details:=jsonb_build_object('outcome','success','target',target_id::text,'username',target_name,'role',role_name,'active',enabled,
  'customer_access',COALESCE(command->'grants','[]'::jsonb));
 INSERT INTO billing_auditevent(at,actor,action,details) VALUES(event_time,actor_name,'User '||action,details) RETURNING id INTO event_id;
 RETURN jsonb_build_object('id',event_id,'at',event_time,'actor',actor_name,'action','User '||action,'details',details,'user_id',target_id,'customer','');
END $$;
REVOKE ALL ON FUNCTION billing_admin_user(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing_admin_user(jsonb) TO billing_web;
