"""The model evaluation (plan 08): does a provider's model pick the right tool,
with the right arguments, and say little enough about it?

    python -m tools.eval.run --provider gemini --secrets <path to secrets.json>

It runs outside the emulator, against a real provider with a real key, and it
costs requests. It is never part of `tools.emulator.headless`. The headless
suite checks the scoring and the runner against the fake provider instead.
"""
