from django.core.management.base import BaseCommand
from billing.monitoring import scan,deliver
class Command(BaseCommand):
    help='Create deduplicated operational alerts; delivery is disabled unless explicitly configured.'
    def add_arguments(self,p):p.add_argument('--deliver',action='store_true')
    def handle(self,*args,**o):
        self.stdout.write(f'New alerts: {scan()}')
        if o['deliver']:self.stdout.write(f'Delivered: {deliver()}')
