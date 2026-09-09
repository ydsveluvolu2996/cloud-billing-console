"""Capture synthetic desktop/mobile flows with an isolated browser profile."""
import os,json,time
from pathlib import Path
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
import django
django.setup()
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1];creds=json.loads((root/'.deployment/review.json').read_text())
out=root/'docs/evidence/ui';out.mkdir(parents=True,exist_ok=True)
results=[];errors=[]
device=TOTPDevice.objects.get(pk=creds['device_id'])
with sync_playwright() as p:
 browser=p.chromium.launch(channel='chrome',headless=True)
 page=browser.new_page(viewport={'width':1440,'height':1000})
 page.on('pageerror',lambda error:errors.append(str(error)))
 base='http://127.0.0.1:8766'
 page.goto(base+'/login/');page.screenshot(path=str(out/'login-desktop.png'),full_page=True)
 page.locator('[name=username]').fill(creds['username']);page.locator('[name=password]').fill(creds['password']);page.get_by_role('button',name='Sign in',exact=True).click()
 page.wait_for_url('**/mfa/');page.screenshot(path=str(out/'mfa-challenge-desktop.png'),full_page=True)
 token=str(totp(device.bin_key,step=device.step,t0=device.t0,digits=device.digits)).zfill(device.digits)
 page.locator('[name=token]').fill(token);page.get_by_role('button',name='Verify',exact=True).click();page.wait_for_url(base+'/')
 paths=[('connection',f'/sources/{creds["source"]}/'),('bulk-onboarding','/onboarding/bulk/'),('budgets','/budgets/'),('customers','/customers/'),('customer',f'/customers/{creds["customer"]}/'),('manual-iam',f'/sources/{creds["source"]}/setup/'),('approval',f'/customers/{creds["customer"]}/governance/'),('alliance','/alliance/'),('operations','/operations/'),('cost-explorer','/')]
 for width in (1440,390):
  page.set_viewport_size({'width':width,'height':1000 if width>1000 else 844})
  for name,path in paths:
   start=time.perf_counter();response=page.goto(base+path);page.wait_for_load_state('networkidle')
   if name=='customers':
    page.locator('[data-customer-tree] summary').first.click()
    page.wait_for_selector('[data-tree-content] .portfolio-connection')
   overflow=page.evaluate('document.documentElement.scrollWidth>window.innerWidth+1')
   results.append({'flow':name,'width':width,'status':response.status,'overflow':overflow,'milliseconds':round((time.perf_counter()-start)*1000)})
   page.screenshot(path=str(out/f'{name}-{width}.png'),full_page=True)
 browser.close()
report={'synthetic':True,'browser':'Installed Chrome, isolated headless profile','flows':results,'page_errors':errors}
(root/'docs/evidence/ui-results.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
if errors or any(x['status']!=200 or x['overflow'] for x in results):raise SystemExit('UI verification failed')
