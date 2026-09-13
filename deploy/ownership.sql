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
