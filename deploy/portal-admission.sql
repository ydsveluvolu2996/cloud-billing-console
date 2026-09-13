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
