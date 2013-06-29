import sys
import time
import array
import ctypes
import numpy as np
from types import ModuleType
from os.path import realpath, dirname, join

from .bind import wrap_llvm_module

import llvm.ee as le
from llvm.workaround.avx_support import detect_avx_support

_nptypemap = {
    'c' : ctypes.c_char,
    'b' : ctypes.c_ubyte,
    'B' : ctypes.c_byte,
    'h' : ctypes.c_short,
    'H' : ctypes.c_ushort,
    'i' : ctypes.c_int,
    'I' : ctypes.c_uint,
    'l' : ctypes.c_long,
    'L' : ctypes.c_ulong,
    'f' : ctypes.c_float,
    'd' : ctypes.c_double,
}

# limited for now
_pytypemap = {
    int   : ctypes.c_int,
    float : ctypes.c_double,
    bool  : ctypes.c_byte,
}

class Array(ctypes.Structure):
    _fields_ = [
        ("data"    , ctypes.c_void_p),
        ("nd"      , ctypes.c_int),
        ("strides" , ctypes.POINTER(ctypes.c_int)),
    ]

class Array_C(ctypes.Structure):
    _fields_ = [
        ("data"  , ctypes.c_void_p),
        ("shape" , ctypes.POINTER(ctypes.c_int)),
    ]

class Array_F(ctypes.Structure):
    _fields_ = [
        ("data"  , ctypes.c_void_p),
        ("shape" , ctypes.POINTER(ctypes.c_int)),
    ]

#------------------------------------------------------------------------
# Arguments
#------------------------------------------------------------------------

def strided_decon(na):
    ctype = _nptypemap[na.dtype.char]
    strideelts = ((s/na.dtype.itemsize) for s in na.strides)

    data = na.ctypes.data_as(ctypes.POINTER(ctype))
    nd = len(na.strides)
    strides = (ctypes.c_int*nd)(*strideelts)
    return data, nd, strides

def contig_decon(na):
    ctype = _nptypemap[na.dtype.char]
    shapeelts = na.shape

    data = na.ctypes.data_as(ctypes.POINTER(ctype))
    nd = len(na.shape)
    strides = (ctypes.c_int*nd)(*shapeelts)
    return data, nd, strides

def adapt(arg, val):
    """
    Adapt arguments to pass to ExecutionEngine.
    """

    if isinstance(val, np.ndarray):
        ndarray = arg._type_ # auto-generated by bind

        if val.flags.contiguous:
            data, nd, shape = contig_decon(val)
            return ndarray(data, shape)
        else:
            data, nd, strides = strided_decon(val)
            return ndarray(data, nd, strides)

    elif isinstance(val, array.array):
        raise NotImplementedError

    elif isinstance(val, list):
        # Build an array from an iterable, not the best thing to
        # do performance-wise usually...
        ndarray = arg._type_ # auto-generated by bind

        ctype = _pytypemap[type(val[0])]

        data = (ctype*len(val))(*val)
        dims = 1
        strides = (ctypes.c_int*1)(len(val))

        return ndarray(data, dims, strides)

    elif isinstance(val, (int, str, float)):
        return val

    elif isinstance(val, tuple):
        return arg._type_(*val)

    elif isinstance(val, ctypes.Structure):
        return ctypes.cast(ctypes.pointer(val), arg._type_)

    else:
        return ctypes.c_char_p(id(val))

def wrap_arguments(fn, args):
    if args:
        largs = list(map(adapt, fn.argtypes, args))
    else:
        largs = ()
    return largs

#------------------------------------------------------------------------
# Toplevel
#------------------------------------------------------------------------

engine = None

class Context(object):

    def __init__(self, env, libs=None):
        self.destroyed = False
        libs = libs or ['prelude']

        for lib in libs:
            if 'darwin' in sys.platform:
                prelude = join(dirname(realpath(__file__)), lib + '.dylib')
            elif 'linux' in sys.platform:
                prelude = join(dirname(realpath(__file__)), lib+ '.so')
            else:
                raise NotImplementedError

            # XXX: yeah, don't do this
            ctypes._dlopen(prelude, ctypes.RTLD_GLOBAL)

        cgen = env['cgen']

        self.__namespace = cgen.globals
        self.__llmodule = cgen.module

        if not detect_avx_support():
            tc = le.TargetMachine.new(features='-avx', cm=le.CM_JITDEFAULT)
        else:
            tc = le.TargetMachine.new(features='', cm=le.CM_JITDEFAULT)

        eb = le.EngineBuilder.new(self.__llmodule)
        self.__engine = eb.create(tc)
        #self.__engine.run_function(cgen.globals['__module'], [])

        mod = ModuleType('blir_wrapper')
        wrap_llvm_module(cgen.module, self.__engine, mod)

        mod.__doc__ = 'Compiled LLVM wrapper module'
        self.__mod = mod

    def lookup_fn(self, fname):
        if not self.destroyed:
            return getattr(self.__mod, fname)
        else:
            raise RuntimeError("Context already destroyed")

    def lookup_fnptr(self, fname):
        if not self.destoryed:
            return self.__engine.get_pointer_to_function(self.__namespace[fname])
        else:
            raise RuntimeError("Context already destroyed")

    def fn(self, fname):
        def wrapper(*args):
            return execute(self, args=args, fname=fname)
        return wrapper

    @property
    def mod(self):
        return self.__mod

    def destroy(self):
        if not self.destroyed:
            self.__engine = None
            self.__llmodule = None
            self.destroyed = True
        else:
            raise RuntimeError("Context already destroyed")

#------------------------------------------------------------------------
# Execution
#------------------------------------------------------------------------

# mostly just delegates to Bitey because I don't feel like rolling a
# ctypes wrapper, this is just for debugging anyways so whatever.
def execute(ctx, args=None, fname=None, timing=False):

    args = args or ()

    try:
        lfn = ctx.lookup_fn(fname or 'main')
    except AttributeError:
        raise Exception("'%s' function not found in module.", fname)
    largs = wrap_arguments(lfn, args)

    if timing:
        start = time.time()

    res = None
    if len(lfn.argtypes) == 0:
        res = lfn()
    elif len(lfn.argtypes) == len(args):
        res = lfn(*largs)
    else:
        print('Invalid number of arguments to main function.')

    if timing:
        print('Time %.6f' % (time.time() - start))

    return res