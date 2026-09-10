#!/usr/bin/env python3
"""Obtain job-scoped AWS credentials without a stored AWS access key."""
import base64
import json
import os
from pathlib import Path
import urllib.request

import boto3


def main():
    url = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'] + '&audience=sts.amazonaws.com'
    request = urllib.request.Request(url, headers={'Authorization': 'bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
    # The URL is the GitHub-provided HTTPS OIDC endpoint.
    with urllib.request.urlopen(request, timeout=20) as response:  # nosec B310
        token = json.load(response)['value']
    payload = token.split('.')[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    # These are diagnostics only. AWS validates the token's signature and trust policy.
    print(json.dumps({k: claims.get(k) for k in ('iss', 'aud', 'sub', 'repository_id', 'ref', 'environment')}))
    if os.environ.get('IDENTITY_ONLY') == 'true':
        return
    role = os.environ.get('BILLING_RELEASE_ROLE_ARN', '')
    if not role:
        raise SystemExit('One-time AWS bootstrap is required: BILLING_RELEASE_ROLE_ARN is unset.')
    response = boto3.client('sts', region_name=os.environ['AWS_REGION']).assume_role_with_web_identity(
        RoleArn=role, RoleSessionName='billing-' + os.environ['GITHUB_RUN_ID'],
        WebIdentityToken=token, DurationSeconds=7200)
    credentials = response['Credentials']
    values = {'AWS_ACCESS_KEY_ID': credentials['AccessKeyId'], 'AWS_SECRET_ACCESS_KEY': credentials['SecretAccessKey'],
              'AWS_SESSION_TOKEN': credentials['SessionToken']}
    with Path(os.environ['GITHUB_ENV']).open('a') as output:
        for key, value in values.items():
            print('::add-mask::' + value)
            output.write(key + '=' + value + '\n')


if __name__ == '__main__':
    main()
