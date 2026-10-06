def continuation_direction(price, window):
    if (window.coverage!="COMPLETE" or window.trend not in {"LONG","SHORT"}
        or price is None or window.support is None or window.resistance is None):
        return None
    if window.support<price<window.resistance:return window.trend
    return None
