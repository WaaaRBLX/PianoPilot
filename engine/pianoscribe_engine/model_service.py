"""Stand-in for Pianoscribe's model_service. PianoPilot always passes the model path
explicitly, so the engine never needs to look one up."""


def get_existing_model_bundle_dir():
    return None
