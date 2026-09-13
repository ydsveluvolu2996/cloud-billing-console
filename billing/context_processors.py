"""Navigation reflects the same request scope used by the data boundary."""
from django.conf import settings
from .access import current_access


def workspace_access(request):
    scope = current_access.get()
    manage = bool(scope and (scope.portfolio or scope.editable))
    if not settings.ENFORCE_CUSTOMER_AUTHORIZATION:
        manage = bool(request.user.is_authenticated and request.user.is_staff)
    return {'can_manage_workspace': manage}
