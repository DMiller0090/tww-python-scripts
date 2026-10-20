"""Native macOS scale queries, loaded only by the Mac hardware path."""
import ctypes
import math
import os


class _Point(ctypes.Structure):
    _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]


class _Size(ctypes.Structure):
    _fields_ = [('width', ctypes.c_double), ('height', ctypes.c_double)]


class _Rect(ctypes.Structure):
    _fields_ = [('origin', _Point), ('size', _Size)]


def _appkit_scale(display):
    """Use NSScreen's backing scale in Dolphin's initialized GUI runtime."""
    appkit = ctypes.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit',
                        mode=getattr(os, 'RTLD_NOLOAD', 0x10))
    if not ctypes.c_void_p.in_dll(appkit, 'NSApp').value:
        return None
    objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    selector = lambda name: objc.sel_registerName(name.encode('ascii'))
    ptr = ctypes.c_void_p
    send = ctypes.CFUNCTYPE(ptr, ptr, ptr)(('objc_msgSend', objc))
    send_arg = ctypes.CFUNCTYPE(ptr, ptr, ptr, ptr)(('objc_msgSend', objc))
    send_index = ctypes.CFUNCTYPE(ptr, ptr, ptr, ctypes.c_size_t)(('objc_msgSend', objc))
    send_count = ctypes.CFUNCTYPE(ctypes.c_size_t, ptr, ptr)(('objc_msgSend', objc))
    send_number = ctypes.CFUNCTYPE(ctypes.c_uint32, ptr, ptr)(('objc_msgSend', objc))
    send_scale = ctypes.CFUNCTYPE(ctypes.c_double, ptr, ptr)(('objc_msgSend', objc))
    send_string = ctypes.CFUNCTYPE(ptr, ptr, ptr, ctypes.c_char_p)(('objc_msgSend', objc))
    pool = send(send(objc.objc_getClass(b'NSAutoreleasePool'), selector('alloc')), selector('init'))
    try:
        screen_class = objc.objc_getClass(b'NSScreen')
        screens = send(screen_class, selector('screens'))
        key = send_string(objc.objc_getClass(b'NSString'), selector('stringWithUTF8String:'), b'NSScreenNumber')
        count = send_count(screens, selector('count')) if screens else 0
        for index in range(min(count, 32)):
            screen = send_index(screens, selector('objectAtIndex:'), index)
            description = send(screen, selector('deviceDescription'))
            number = send_arg(description, selector('objectForKey:'), key)
            if number and send_number(number, selector('unsignedIntValue')) == display:
                return float(send_scale(screen, selector('backingScaleFactor')))
        if not display and count == 1:
            screen = send_index(screens, selector('objectAtIndex:'), 0)
            return float(send_scale(screen, selector('backingScaleFactor')))
        return None
    finally:
        if pool:
            send(pool, selector('drain'))


def _display_scale(geometry):
    """Read the backing scale of the display containing most of the window."""
    cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
    cg.CGMainDisplayID.restype = ctypes.c_uint32
    cg.CGGetActiveDisplayList.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                        ctypes.POINTER(ctypes.c_uint32)]
    cg.CGGetActiveDisplayList.restype = ctypes.c_int32
    cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
    cg.CGDisplayBounds.restype = _Rect
    cg.CGDisplayCopyDisplayMode.argtypes = [ctypes.c_uint32]
    cg.CGDisplayCopyDisplayMode.restype = ctypes.c_void_p
    for name in ('CGDisplayModeGetWidth', 'CGDisplayModeGetPixelWidth'):
        function = getattr(cg, name)
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_size_t
    cg.CGDisplayModeRelease.argtypes = [ctypes.c_void_p]
    display = cg.CGMainDisplayID()
    if geometry is not None:
        displays = (ctypes.c_uint32 * 32)()
        count = ctypes.c_uint32()
        if cg.CGGetActiveDisplayList(32, displays, ctypes.byref(count)) == 0:
            x, y, width, height = geometry
            largest = 0
            for candidate in displays[:min(count.value, 32)]:
                bounds = cg.CGDisplayBounds(candidate)
                area = (max(0, min(x+width, bounds.origin.x+bounds.size.width)-max(x,bounds.origin.x)) *
                        max(0, min(y+height, bounds.origin.y+bounds.size.height)-max(y,bounds.origin.y)))
                if area > largest:
                    display, largest = candidate, area
    try:
        backing = _appkit_scale(display)
        if backing is not None and math.isfinite(backing) and 1.0 <= backing <= 4.0:
            return backing
    except Exception:
        pass
    mode = cg.CGDisplayCopyDisplayMode(display)
    if not mode:
        raise RuntimeError('macOS display scale unavailable')
    try:
        logical = cg.CGDisplayModeGetWidth(mode)
        physical = cg.CGDisplayModeGetPixelWidth(mode)
        return physical / logical if logical else 1.0
    finally:
        cg.CGDisplayModeRelease(mode)


