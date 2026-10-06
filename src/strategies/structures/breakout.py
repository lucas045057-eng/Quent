from decimal import Decimal as D

def breakout_direction(price, window):
    if window.coverage!="COMPLETE" or price is None:return None
    if window.resistance is not None and price>window.resistance:return "LONG"
    if window.support is not None and price<window.support:return "SHORT"
    return None
