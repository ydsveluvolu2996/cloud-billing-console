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
