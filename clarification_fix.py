from __future__ import annotations

import re

_INSTALLED = False


def decisive_answer(question: str, answer: str) -> bool:
    reply = re.sub(r"\s+", " ", answer.strip().lower())
    if not reply:
        return False
    patterns = [
        r"additional item",
        r"separate item",
        r"separate line item",
        r"already included",
        r"already part",
        r"not included",
        r"do not include",
        r"^yes$",
        r"^no$",
        r"^correct$",
        r"^confirm(?:ed)?$",
        r"^ok(?:ay)?$",
        r"^proceed$",
        r"^go ahead$",
        r"^all correct$",
        r"^(?:confirm(?:ed)?|ok(?:ay)?|yes|correct)\s+\$?\d+(?:\.\d{1,2})?$",
    ]
    if any(re.search(pattern, reply) for pattern in patterns):
        return True
    return " or " in question.lower() and len(reply.split()) <= 10


def final_instruction(answer: str) -> str:
    reply = answer.strip().lower()
    if "additional item" in reply or "separate" in reply:
        return "Treat the labour charge as a separate additional line item on top of the installation amount."
    if "already included" in reply or "already part" in reply:
        return "The charge is already included in the installation total and must not be added again."
    if decisive_answer("", answer):
        return "Use the commercially natural interpretation of the supplied quantities and prices and create the draft now."
    return f"Apply the user's clarification exactly: {answer.strip()}"


def install_clarification_fix() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    import whatsapp_routes as wa

    async def fixed_handler(sender: str, incoming_text: str) -> bool:
        pending = wa.get_whatsapp_clarification(sender)
        if not pending:
            return False

        if wa.looks_like_new_document_request(incoming_text):
            wa.clear_whatsapp_clarification(sender)
            return False

        original = str(pending["original_text"])
        flow_type = str(pending["flow_type"])
        gst_rate = wa.explicit_gst_confirmation(original, incoming_text)
        is_decisive = decisive_answer(original, incoming_text)

        combined = f"{original}\nClarification answer: {incoming_text.strip()}"
        if gst_rate is not None:
            combined += f"\nConfirmed GST rate: {gst_rate:g} percent."
        if is_decisive:
            combined += (
                "\nFinal instruction: " + final_instruction(incoming_text)
                + " Do not ask another question about details that can be calculated from the supplied values."
            )

        await wa.send_whatsapp_text(sender, "⏳ Applying your answer to the existing draft…")
        wa.check_ai_rate_limit("whatsapp", sender)
        parsed = await wa.ai_parse(combined)

        if gst_rate is not None:
            parsed.gst_rate_percent = gst_rate
            parsed.clarification_needed = False
            parsed.clarification_question = ""

        if is_decisive and any(float(item.unit_price or 0) > 0 for item in parsed.items):
            parsed.clarification_needed = False
            parsed.clarification_question = ""

        if parsed.clarification_needed:
            wa.save_whatsapp_clarification(sender, flow_type, original)
            await wa.send_whatsapp_text(
                sender,
                parsed.clarification_question or "Please provide the one remaining required price or quantity.",
            )
            return True

        wa.clear_whatsapp_clarification(sender)
        if flow_type == "quote":
            quote = wa.create_ai_quote(combined, parsed)
            await wa.send_whatsapp_text(sender, wa.quote_summary(quote))
            await wa.send_quote_action_list(sender, quote)
        else:
            invoice = wa.create_ai_invoice(combined, parsed)
            await wa.send_whatsapp_text(sender, wa.invoice_summary(invoice))
            await wa.send_invoice_action_buttons(sender, invoice)
        return True

    wa.handle_pending_whatsapp_clarification = fixed_handler
    wa._clarification_loop_fix_installed = True
    _INSTALLED = True
