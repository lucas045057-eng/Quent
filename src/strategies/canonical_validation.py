"""Fresh DB-read validation of immutable facts without changing source clocks."""
PROTOCOL='V2_CANONICAL_REPEATABLE_READ_V1'

def attest_canonical_read(observation, *, checked_at):
    if (not observation.usable or observation.provider!='bitget'
        or not observation.source_ref.startswith('canonical:') or observation.source_context is not None
        or observation.fetched_at is None or observation.observed_at is None
        or observation.fetched_at>checked_at or observation.observed_at>checked_at):
        return observation
    return observation.model_copy(update={'revalidated_at':checked_at,'source_context':{'canonical_validation':{
        'protocol':PROTOCOL,'checked_at':checked_at.isoformat(),'base_fact_digest':observation.digest,
        'source_ref':observation.source_ref,'source_fetched_at':observation.fetched_at.isoformat(),
        'source_observed_at':observation.observed_at.isoformat()}}})

def valid_canonical_read(observation, *, requested_at, captured_at):
    try:
        checked=observation.revalidated_at
        if (checked is None or not requested_at<=checked<=captured_at or not observation.usable
            or observation.provider!='bitget' or not observation.source_ref.startswith('canonical:')
            or set(observation.source_context)!={'canonical_validation'}):return False
        proof=observation.source_context['canonical_validation']
        original=observation.model_copy(update={'revalidated_at':None,'source_context':None})
        return (proof['protocol']==PROTOCOL and proof['checked_at']==checked.isoformat()
            and proof['base_fact_digest']==original.digest and proof['source_ref']==observation.source_ref
            and proof['source_fetched_at']==observation.fetched_at.isoformat()
            and proof['source_observed_at']==observation.observed_at.isoformat()
            and observation.fetched_at<=checked and observation.observed_at<=checked)
    except (KeyError,TypeError,AttributeError,ValueError):return False

def fresh_receipt(observation, *, requested_at, captured_at):
    return (observation.fetched_at is not None and requested_at<=observation.fetched_at<=captured_at
        or valid_canonical_read(observation,requested_at=requested_at,captured_at=captured_at))
