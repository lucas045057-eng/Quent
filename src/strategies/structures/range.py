"""Range boundaries may be watched even without a legacy trend alignment."""
def boundary(window, side):
    if window.coverage!="COMPLETE":return None
    return window.resistance if side=="LONG" else window.support
