from pathlib import Path
p=Path('/mnt/data/work_v93/oiapp/journal/journal_routes.py')
s=p.read_text()
# insert helper after _entry_net_points_from_legs
needle = '''def _trade_net_premium(t):\n'''
helper = r'''
def _apply_net_premium_to_option_legs(legs, net_points):
    """Return a copy of option legs whose prices encode a requested net.

    This is used when the user enters only the combined roll order cashflow
    instead of explicit new-leg prices.  The synthetic prices keep the journal
    math internally consistent without pretending we know the true leg fills.
    """
    out = [dict(l or {}) for l in (legs or [])]
    try:
        net = float(net_points or 0)
    except Exception:
        net = 0.0
    opt_indices = [i for i, l in enumerate(out) if str(l.get("option_type", "")).lower() in ("call", "put")]
    if not opt_indices:
        return out
    for i in opt_indices:
        out[i]["price"] = 0.0
    sell_i = next((i for i in opt_indices if str(out[i].get("side", "")).lower() == "sell"), opt_indices[0])
    buy_i = next((i for i in opt_indices if str(out[i].get("side", "")).lower() == "buy"), opt_indices[-1])
    if net >= 0:
        out[sell_i]["price"] = round(abs(net), 4)
    else:
        out[buy_i]["price"] = round(abs(net), 4)
    return out

'''
if helper.strip() not in s:
    s=s.replace(needle, helper+needle)
# Patch roll_trade calculation block
old = '''            new_legs = [dict(l or {}) for l in (d.get("new_legs") or [])]
            if not new_legs:
                con.rollback(); con.close(); return jsonify({"error": "new_legs required for structured roll"}), 400
            new_exp = d.get("new_expiry") or next((l.get("expiry") for l in new_legs if l.get("expiry")), "")
            for l in new_legs:
                if new_exp and not l.get("expiry"):
                    l["expiry"] = new_exp
                if not l.get("qty"):
                    l["qty"] = roll_qty
            new_type = _infer_trade_type_from_legs(new_legs, src.get("trade_type") or "Custom")
            nf = _fields_from_legs(new_legs, new_type, roll_qty)
            new_qty = _option_qty_from_legs(new_legs, roll_qty)
            new_entry_net = _entry_net_points_from_legs(new_legs, new_qty) or 0.0
            roll_adjustment = float(new_entry_net or 0) - float(close_net or 0)
            cumulative_basis = float(old_side_net or 0) + float(roll_adjustment or 0)
            basis_note = (f"Rolled from #{tid}; closed side event #{closed_id}; old side basis {old_side_net:.2f}; "
                          f"close side cost/net {float(close_net or 0):.2f}; new signed premium {new_entry_net:+.2f}; "
                          f"roll adjustment {roll_adjustment:+.2f} (positive=extra credit, negative=debit paid); "
                          f"effective side basis {cumulative_basis:+.2f}. {roll_reason}")
'''
new = '''            new_legs = [dict(l or {}) for l in (d.get("new_legs") or [])]
            if not new_legs:
                con.rollback(); con.close(); return jsonify({"error": "new_legs required for structured roll"}), 400
            new_exp = d.get("new_expiry") or next((l.get("expiry") for l in new_legs if l.get("expiry")), "")
            for l in new_legs:
                if new_exp and not l.get("expiry"):
                    l["expiry"] = new_exp
                if not l.get("qty"):
                    l["qty"] = roll_qty

            # Roll premium semantics:
            #   - signed_roll_adjustment / net_roll_credit_debit is the TOTAL roll-order cashflow.
            #     Positive = extra credit collected; negative = debit paid.
            #   - This value is added directly to the old side basis.  It must NOT be reduced
            #     by the close cost again, otherwise a -1.03 roll debit becomes -3.53 when
            #     close cost is 2.50.
            #   - The new trade uses the actual/implied new opening net so realized close P&L
            #     plus new open credit stays mathematically correct.  Effective carry basis is
            #     preserved in the note/API response.
            explicit_roll_cashflow = False
            roll_adjustment = None
            for _rk in ("signed_roll_adjustment", "net_roll_credit_debit", "roll_adjustment_signed"):
                if d.get(_rk) not in (None, ""):
                    try:
                        roll_adjustment = float(d.get(_rk) or 0)
                        explicit_roll_cashflow = True
                        break
                    except Exception:
                        roll_adjustment = None
            new_qty = _option_qty_from_legs(new_legs, roll_qty)
            actual_new_entry_net = _entry_net_points_from_legs(new_legs, new_qty) or 0.0
            if explicit_roll_cashflow or bool(d.get("roll_net_is_cashflow")):
                if roll_adjustment is None:
                    try:
                        roll_adjustment = float(d.get("signed_new_net") or 0)
                    except Exception:
                        roll_adjustment = 0.0
                implied_new_open_net = float(close_net or 0) + float(roll_adjustment or 0)
                # If the user did not provide actual new-leg prices, encode the implied new
                # opening net into the synthetic new legs so the open trade tracks correctly.
                if not bool(d.get("new_leg_prices_provided")):
                    new_legs = _apply_net_premium_to_option_legs(new_legs, implied_new_open_net)
                    new_qty = _option_qty_from_legs(new_legs, roll_qty)
                    actual_new_entry_net = _entry_net_points_from_legs(new_legs, new_qty) or implied_new_open_net
            else:
                implied_new_open_net = actual_new_entry_net
                roll_adjustment = float(actual_new_entry_net or 0) - float(close_net or 0)
            cumulative_basis = float(old_side_net or 0) + float(roll_adjustment or 0)

            new_type = _infer_trade_type_from_legs(new_legs, src.get("trade_type") or "Custom")
            nf = _fields_from_legs(new_legs, new_type, roll_qty)
            basis_note = (f"Rolled from #{tid}; closed side event #{closed_id}; old side basis {old_side_net:.2f}; "
                          f"close side cost/net {float(close_net or 0):.2f}; actual/implied new open net {actual_new_entry_net:+.2f}; "
                          f"net roll cashflow {roll_adjustment:+.2f} (positive=credit, negative=debit); "
                          f"effective position credit {cumulative_basis:+.2f}. {roll_reason}")
'''
if old not in s:
    raise SystemExit('target block not found')
s=s.replace(old,new)
# Patch response to include actual/implied and names
s=s.replace('''                "roll_adjustment": round(roll_adjustment, 2),
                "cumulative_side_basis": round(cumulative_basis, 2),
                "message": "Roll split complete: unrolled side remains open; rolled side opened as a new trade.",
''','''                "roll_adjustment": round(roll_adjustment, 2),
                "net_roll_cashflow": round(roll_adjustment, 2),
                "actual_new_entry_net": round(actual_new_entry_net, 2),
                "implied_new_open_net": round(implied_new_open_net, 2),
                "cumulative_side_basis": round(cumulative_basis, 2),
                "effective_position_credit": round(cumulative_basis, 2),
                "message": "Roll split complete: unrolled side remains open; rolled side opened as a new trade.",
''')
p.write_text(s)
