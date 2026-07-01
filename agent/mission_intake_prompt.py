"""Mission Intake Loop — prompt builder for /missao.

Transforms a raw request into a self-contained mission-intake prompt
that the agent can execute (via clarify, loop, SDD, TDD, Kanban, or Cron).
The prompt guides the agent through:

1. Classify the user's intent
2. Use clarify for missing fields
3. Build a Mission Contract
4. Route to execution mode
5. Validate and report evidence

No engine, no model call — this is a pure text transformation that
the live agent processes as a normal turn.
"""

from __future__ import annotations

_MISSION_INTAKE_PROMPT_TEMPLATE = """\
# Mission Intake

You are about to receive a user request that should be treated as a **mission** — not a simple query.

## Your job

1. **Classify** the type of mission the user is giving you.
2. **Ask clarifying questions** via `clarify` ONLY for fields that are genuinely missing and needed to execute safely.
3. Once enough information is available, **draft a Mission Contract** as a YAML block in the conversation.
4. **Confirm the contract** with the user before executing.
5. **Route to the correct execution mode**: normal agent loop, TDD, SDD (subagent-driven), Kanban, Cron, or one-shot.
6. **Execute** the mission following the contract.
7. **Validate** using the contract's success criteria.
8. **Report** evidence and any remaining gates.

## Mission Contract schema

Build a YAML contract with at minimum:

```yaml
mission:
  id: M-YYYYMMDD-short
  objetivo: ""
  tipo: one_shot | build | review | investigate | recurring_agent | cron | kanban
  valor: caixa | alavancagem | velocidade | protecao | automacao | aprendizado
  perfil_executor: dev-orch | nyx | hunter | worker
  autonomia: A0 | A1 | A2 | A3 | A4 | A5
  superficies_permitidas: []
  superficies_negadas: []
  sucesso:
    contrato: ""
    verificador: ""
    zero_resultado_falha: true
  gates:
    requeridos: []
    aprovados: []
  evidencias:
    destino: ""
  reporting:
    destino: telegram
    frequencia: final | daily | weekly | action_required
```

## Autonomy levels

Always confirm with the user before setting autonomy above A2:

- **A0** — analysis only, no file/tool execution
- **A1** — read-only: inspect files, config, logs; no writes
- **A2** — local writes: create/modify drafts, local files, kanban cards
- **A3** — runtime changes pending gate: cron, worker, kanban activation
- **A4** — external sends pending gate: Telegram messages, email drafts
- **A5** — autonomous: all the above, external sends, payments, deploys

## Safety rules

- **NEVER** access, create, or modify: secrets, credentials, tokens, cookies, auth files, provider/auth/fallback config, gateway restart, Telegram/Discord token/channel/thread, cron activation
- **ALWAYS** use gates for: production deploy, Mind-Sync/Nyx-Ops promotion, external account actions, payments, real-data memory ingestion into L3 stores
- When touching code or infrastructure, **always verify real output** before claiming completion
- **Never fabricate** outputs, secrets, file contents, model capabilities, or runtime state

## Clarify strategy

Use `clarify` up to 4 choices at a time. Ask in this order:

1. What type of mission?
2. What is the expected outcome?
3. Where am I allowed to act (surfaces)?
4. What autonomy level?
5. What success verifier?
6. Any schedule / recurrence?
7. What gates do you approve now?
8. How to report?

If the user already provided enough info to fill a field, skip it.

## Source context

{source_line}

## User request

{raw_request}

---

Now proceed with classifying this mission and filling in the contract.
"""


def build_mission_intake_prompt(raw_request: str, source: str = "") -> str:
    """Build a self-contained mission-intake prompt.

    Args:
        raw_request: The user's original /missao argument text (may be empty).
        source: Optional platform/source identifier (e.g. "Telegram", "Discord").

    Returns:
        A fully-formed prompt string that the agent processes as a normal turn.
    """
    # Normalize empty/whitespace-only requests
    stripped = raw_request.strip()
    effective_request = stripped if stripped else raw_request.strip()

    # Build source line
    if source:
        source_line = f"Source: {source}"
    else:
        source_line = "Source: (not specified)"

    # If the request is empty, add a fallback instruction
    if not effective_request:
        effective_request = "(no specific request — open-ended mission intake)"

    return _MISSION_INTAKE_PROMPT_TEMPLATE.format(
        source_line=source_line,
        raw_request=effective_request,
    )
