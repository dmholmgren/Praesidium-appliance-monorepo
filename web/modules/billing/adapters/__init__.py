"""Billing module adapters."""
# Existing adapters
from modules.billing.adapters.manictime import ManicTimeAdapter
from modules.billing.adapters.freepbx import FreePBXAdapter
from modules.billing.adapters.dms_time import DMSTimeAdapter
from modules.billing.adapters.qbo_export import QBOExportAdapter
from modules.billing.adapters.ledes import LEDESExporter

# Payment gateway base
from modules.billing.adapters.payment_gateway_base import (
    PaymentGatewayBase,
    GatewayStatus,
    GatewayCapability,
    ConnectorHealth,
    DiagnosisCheck,
    DiagnosisReport,
    TestResult,
    PaymentRequest,
    PaymentResult,
    WebhookEvent,
)

# Payment gateways (replace old LawPayAdapter stub)
from modules.billing.adapters.lawpay_gateway import LawPayGateway
from modules.billing.adapters.braintree_gateway import BraintreeGateway
from modules.billing.adapters.paypal_gateway import PayPalGateway

# Accounting gateways
from modules.billing.adapters.qbo_gateway import QBOGateway

# Backward compat — old code references LawPayAdapter
LawPayAdapter = LawPayGateway
