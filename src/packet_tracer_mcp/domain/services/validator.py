"""
Validador de planes de topología.

Usa las reglas de domain/rules/ con errores tipados.
"""

from __future__ import annotations
from ..models.plans import TopologyPlan
from ..models.errors import ValidationResult
from ..rules.device_rules import validate_devices
from ..rules.ip_rules import validate_ips, validate_dhcp
from ..rules.cable_rules import validate_links
from ..rules.topology_rules import (
    validate_connectivity, validate_routing, validate_wireless,
)


def validate_plan(plan: TopologyPlan) -> ValidationResult:
    """
    Valida un plan completo.
    Retorna un ValidationResult con errores y warnings tipados.
    También actualiza plan.errors y plan.warnings para compatibilidad.
    """
    result = ValidationResult()

    # Dispositivos
    result.errors.extend(validate_devices(plan))

    # Enlaces y cables
    link_errors, link_warnings = validate_links(plan)
    result.errors.extend(link_errors)
    result.warnings.extend(link_warnings)

    # IPs
    result.errors.extend(validate_ips(plan))

    # DHCP
    dhcp_issues = validate_dhcp(plan)
    # DHCP gateway mismatch es warning, no error critical
    for issue in dhcp_issues:
        if issue.code.value == "DHCP_GATEWAY_MISMATCH":
            result.warnings.append(issue)
        else:
            result.errors.append(issue)

    # Forma del grafo: islas y routing incoherente. Va al final porque asume
    # que dispositivos y enlaces ya se validaron de a uno.
    result.errors.extend(validate_connectivity(plan))
    result.errors.extend(validate_routing(plan))
    # Warning: es una limitacion de PT, no un error del plan.
    result.warnings.extend(validate_wireless(plan))

    # Sync con plan.errors/warnings para compatibilidad
    plan.errors = result.error_messages()
    plan.warnings = result.warning_messages()

    return result
