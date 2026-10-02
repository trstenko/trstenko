"""The assistant: everything between "the wake word fired" and "the reply was
spoken".

The pieces are small and each has one job (plan 03): `provider` talks to a
model, `stt` turns audio into text, `loop` runs the tools the model asks for,
`meter` counts what that cost, `tts` speaks. Nothing in this package changes
device state itself. The worker thread posts events to the main thread and
sends it `Command`s (plan 01), which is what keeps a model that answers late,
or never, from being able to do anything to the device.

Importing this package imports nothing third-party and starts nothing.
"""
