"""Pianoscribe's piano transcription engine, packaged for PianoPilot.

piano_engine.py is copied unchanged from Pianoscribe 3.1.1 by Justagwas
(https://github.com/Justagwas/Pianoscribe), licensed GPL-3.0 (LICENSE-GPL-3.0.txt).
The transcription model is TransKun by Yujia Yan, MIT licensed (LICENSE-model-MIT.txt).

resource_service.py, runtime_service.py and model_service.py are small stand-ins written
for PianoPilot: they replace Pianoscribe's app-side services (settings, GPU runtime packs,
model downloads) with the few functions the engine actually calls.
"""
