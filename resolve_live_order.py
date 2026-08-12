'''Explicit operator recovery for orders confirmed absent at the exchange.'''

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from core.order_store import OrderStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Resolve an UNKNOWN or SUBMITTING live order only after independently '
            'confirming that no exchange order exists.'
        )
    )
    parser.add_argument('client_order_id')
    parser.add_argument('--order-store', default='reports/live_orders.db')
    parser.add_argument('--operator', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument(
        '--confirm-not-submitted',
        action='store_true',
        required=True,
        help='Required acknowledgement that the order is absent at the venue',
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    store = OrderStore(args.order_store)
    try:
        order = store.resolve_as_unsubmitted(
            args.client_order_id,
            confirmed_by=args.operator,
            reason=args.reason,
            now=datetime.now(timezone.utc).isoformat(),
        )
        resolution = store.resolutions_for(args.client_order_id)[-1]
        print(json.dumps(
            {'order': order, 'resolution': resolution},
            default=str,
            ensure_ascii=False,
        ))
    finally:
        store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
