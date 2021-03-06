import numpy as np
import numba
from numba import types
from numba.extending import (models, register_model, lower_cast, infer_getattr,
    type_callable, infer)
from numba.typing.templates import (infer_global, AbstractTemplate, signature,
    AttributeTemplate, bound_function)
from numba.typing.arraydecl import (get_array_index_type, _expand_integer,
    ArrayAttribute, SetItemBuffer)
from numba.typing.npydecl import (Numpy_rules_ufunc, NumpyRulesArrayOperator,
    NumpyRulesInplaceArrayOperator, NumpyRulesUnaryArrayOperator,
    NdConstructorLike)
import hpat
from hpat.str_ext import string_type
from hpat.str_arr_ext import (string_array_type, offset_typ, char_typ,
    str_arr_payload_type, StringArrayType, GetItemStringArray)
from hpat.pd_timestamp_ext import pandas_timestamp_type, datetime_date_type

# TODO: implement type inference instead of subtyping array since Pandas as of
# 0.23 is deprecating things like itemsize etc.
# class SeriesType(types.ArrayCompatible):
class SeriesType(types.IterableType):
    """Temporary type class for Series objects.
    """
    # array_priority = 1000
    def __init__(self, dtype, ndim, layout, readonly=False, name=None,
                 aligned=True):
        # same as types.Array, except name is Series, and buffer attributes
        # initialized here
        assert ndim == 1, "Series() should be one dimensional"
        assert name is None
        self.mutable = True
        self.aligned = True
        self.dtype = dtype
        self.ndim = ndim
        self.layout = layout

        if readonly:
            self.mutable = False
        if (not aligned or
            (isinstance(dtype, types.Record) and not dtype.aligned)):
            self.aligned = False
        if name is None:
            type_name = "series"
            if not self.mutable:
                type_name = "readonly " + type_name
            if not self.aligned:
                type_name = "unaligned " + type_name
            name = "%s(%s, %sd, %s)" % (type_name, dtype, ndim, layout)
        super(SeriesType, self).__init__(name=name)

    @property
    def mangling_args(self):
        # same as types.Array
        args = [self.dtype, self.ndim, self.layout,
                'mutable' if self.mutable else 'readonly',
                'aligned' if self.aligned else 'unaligned']
        return self.__class__.__name__, args

    def copy(self, dtype=None, ndim=None, layout=None, readonly=None):
        # same as types.Array, except Series return type
        if dtype is None:
            dtype = self.dtype
        if ndim is None:
            ndim = self.ndim
        if layout is None:
            layout = self.layout
        if readonly is None:
            readonly = not self.mutable
        return SeriesType(dtype=dtype, ndim=ndim, layout=layout, readonly=readonly,
                     aligned=self.aligned)

    @property
    def key(self):
        # same as types.Array
        return self.dtype, self.ndim, self.layout, self.mutable, self.aligned

    def unify(self, typingctx, other):
        # same as types.Array, except returns Series for Series/Series
        # If other is array and the ndim matches
        if isinstance(other, SeriesType) and other.ndim == self.ndim:
            # If dtype matches or other.dtype is undefined (inferred)
            if other.dtype == self.dtype or not other.dtype.is_precise():
                if self.layout == other.layout:
                    layout = self.layout
                else:
                    layout = 'A'
                readonly = not (self.mutable and other.mutable)
                aligned = self.aligned and other.aligned
                return SeriesType(dtype=self.dtype, ndim=self.ndim, layout=layout,
                             readonly=readonly, aligned=aligned)

        # XXX: unify Series/Array as Array
        return super(SeriesType, self).unify(typingctx, other)

    # @property
    # def as_array(self):
    #     return types.Array(self.dtype, self.ndim, self.layout)

    def can_convert_to(self, typingctx, other):
        # same as types.Array, TODO: add Series?
        if (isinstance(other, types.Array) and other.ndim == self.ndim
            and other.dtype == self.dtype):
            if (other.layout in ('A', self.layout)
                and (self.mutable or not other.mutable)
                and (self.aligned or not other.aligned)):
                return types.Conversion.safe

    def is_precise(self):
        # same as types.Array
        return self.dtype.is_precise()

    @property
    def iterator_type(self):
        # same as Buffer
        # TODO: fix timestamp
        return types.iterators.ArrayIterator(self)

    @property
    def is_c_contig(self):
        # same as Buffer
        return self.layout == 'C' or (self.ndim <= 1 and self.layout in 'CF')

    @property
    def is_f_contig(self):
        # same as Buffer
        return self.layout == 'F' or (self.ndim <= 1 and self.layout in 'CF')

    @property
    def is_contig(self):
        # same as Buffer
        return self.layout in 'CF'


string_series_type = SeriesType(string_type, 1, 'C', True)
# TODO: create a separate DatetimeIndex type from Series
dt_index_series_type = SeriesType(types.NPDatetime('ns'), 1, 'C')
date_series_type = SeriesType(datetime_date_type, 1, 'C')

# register_model(SeriesType)(models.ArrayModel)
# need to define model since fix_df_array overload goes to native code
@register_model(SeriesType)
class SeriesModel(models.StructModel):
    def __init__(self, dmm, fe_type):
        # TODO: types other than Array and StringArray?
        if fe_type.dtype == string_type:
            members = [
                ('num_items', types.uint64),
                ('num_total_chars', types.uint64),
                ('offsets', types.CPointer(offset_typ)),
                ('data', types.CPointer(char_typ)),
                ('meminfo', types.MemInfoPointer(str_arr_payload_type)),
            ]
        else:
            ndim = 1
            members = [
                ('meminfo', types.MemInfoPointer(fe_type.dtype)),
                ('parent', types.pyobject),
                ('nitems', types.intp),
                ('itemsize', types.intp),
                ('data', types.CPointer(fe_type.dtype)),
                ('shape', types.UniTuple(types.intp, ndim)),
                ('strides', types.UniTuple(types.intp, ndim)),

            ]

        super(SeriesModel, self).__init__(dmm, fe_type, members)

class BoxedSeriesType(types.Type):
    """Series type before unboxing. Using a different type to avoid data model
    issues and confusion.
    """
    def __init__(self, dtype):
        self.dtype = dtype
        name = "BoxedSeriesType({})".format(dtype)
        super(BoxedSeriesType, self).__init__(name)

# register_model(BoxedSeriesType)(models.OpaqueModel)
register_model(BoxedSeriesType)(SeriesModel)

class UnBoxedSeriesType(types.Type):
    """Series type before boxing. Using a different type to avoid data model
    issues and confusion.
    """
    def __init__(self, dtype):
        self.dtype = dtype
        name = "UnBoxedSeriesType({})".format(dtype)
        super(UnBoxedSeriesType, self).__init__(name)

register_model(UnBoxedSeriesType)(SeriesModel)

def series_to_array_type(typ, replace_boxed=False):
    if typ.dtype == string_type:
        new_typ = string_array_type
    elif isinstance(typ, BoxedSeriesType):
        new_typ = typ
        if replace_boxed:
            new_typ = types.Array(typ.dtype, 1, 'C')
    else:
        # TODO: other types?
        new_typ = types.Array(
        typ.dtype, typ.ndim, typ.layout, not typ.mutable,
        aligned=typ.aligned)
    return new_typ


def arr_to_series_type(arr):
    series_type = None
    if isinstance(arr, types.Array):
        series_type = SeriesType(arr.dtype, arr.ndim, arr.layout,
            not arr.mutable, aligned=arr.aligned)
    elif arr == string_array_type:
        # StringArray is readonly
        series_type = string_series_type
    return series_type

def arr_to_boxed_series_type(arr):
    series_type = None
    if isinstance(arr, types.Array):
        series_type = BoxedSeriesType(arr.dtype)
    elif arr == string_array_type:
        series_type = BoxedSeriesType(string_type)
    return series_type


def if_series_to_array_type(typ, replace_boxed=False):
    if isinstance(typ, SeriesType):
        return series_to_array_type(typ, replace_boxed)
    # XXX: Boxed series variable types shouldn't be replaced in hiframes_typed
    # it results in cast error for call dummy_unbox_series
    if replace_boxed and isinstance(typ, BoxedSeriesType):
        return series_to_array_type(typ, replace_boxed)
    if isinstance(typ, (types.Tuple, types.UniTuple)):
        return types.Tuple(
            [if_series_to_array_type(t, replace_boxed) for t in typ.types])
    # TODO: other types than can have Series inside: list, set, etc.
    return typ

def if_arr_to_series_type(typ):
    if isinstance(typ, types.Array) or typ == string_array_type:
        return arr_to_series_type(typ)
    if isinstance(typ, (types.Tuple, types.UniTuple)):
        return types.Tuple([if_arr_to_series_type(t) for t in typ.types])
    # TODO: other types than can have Arrays inside: list, set, etc.
    return typ

def if_series_to_unbox(typ):
    if isinstance(typ, SeriesType):
        return UnBoxedSeriesType(typ.dtype)

    if isinstance(typ, (types.Tuple, types.UniTuple)):
        return types.Tuple(
            [if_series_to_unbox(t) for t in typ.types])
    # TODO: other types than can have Series inside: list, set, etc.
    return typ

@lower_cast(string_array_type, UnBoxedSeriesType)
@lower_cast(types.Array, UnBoxedSeriesType)
def cast_string_series_unbox(context, builder, fromty, toty, val):
    return val

@lower_cast(string_series_type, string_array_type)
@lower_cast(string_array_type, string_series_type)
def cast_string_series(context, builder, fromty, toty, val):
    return val

@lower_cast(SeriesType, types.Array)
@lower_cast(types.Array, SeriesType)
def cast_series(context, builder, fromty, toty, val):
    return val

# --------------------------------------------------------------------------- #
# --- typing similar to arrays adopted from arraydecl.py, npydecl.py -------- #


@infer_getattr
class SeriesAttribute(AttributeTemplate):
    key = SeriesType

    def resolve_values(self, ary):
        return series_to_array_type(ary, True)

    @bound_function("array.argsort")
    def resolve_argsort(self, ary, args, kws):
        resolver = ArrayAttribute.resolve_argsort.__wrapped__
        sig = resolver(self, ary, args, kws)
        sig.return_type = if_arr_to_series_type(sig.return_type)
        return sig

    @bound_function("array.take")
    def resolve_take(self, ary, args, kws):
        resolver = ArrayAttribute.resolve_take.__wrapped__
        sig = resolver(self, ary, args, kws)
        sig.return_type = if_arr_to_series_type(sig.return_type)
        return sig

# TODO: use ops logic from pandas/core/ops.py
# # called from numba/numpy_support.py:resolve_output_type
# # similar to SmartArray (targets/smartarray.py)
# @type_callable('__array_wrap__')
# def type_series_array_wrap(context):
#     def typer(input_type, result):
#         if isinstance(input_type, SeriesType):
#             return input_type.copy(dtype=result.dtype,
#                                    ndim=result.ndim,
#                                    layout=result.layout)

#     return typer

@infer
class SeriesCompEqual(AbstractTemplate):
    key = '=='
    def generic(self, args, kws):
        from hpat.str_arr_ext import is_str_arr_typ
        assert not kws
        [va, vb] = args
        # if one of the inputs is string array
        if va == string_series_type or vb == string_series_type:
            # inputs should be either string array or string
            assert is_str_arr_typ(va) or va == string_type
            assert is_str_arr_typ(vb) or vb == string_type
            return signature(SeriesType(types.boolean, 1, 'C'), va, vb)

        if ((va == dt_index_series_type and vb == string_type)
                or (vb == dt_index_series_type and va == string_type)):
            return signature(SeriesType(types.boolean, 1, 'C'), va, vb)

@infer
class CmpOpNEqSeries(SeriesCompEqual):
    key = '!='

@infer
class CmpOpGESeries(SeriesCompEqual):
    key = '>='

@infer
class CmpOpGTSeries(SeriesCompEqual):
    key = '>'

@infer
class CmpOpLESeries(SeriesCompEqual):
    key = '<='

@infer
class CmpOpLTSeries(SeriesCompEqual):
    key = '<'

# @infer
# class GetItemBuffer(AbstractTemplate):
#     key = "getitem"

#     def generic(self, args, kws):
#         assert not kws
#         [ary, idx] = args
#         import pdb; pdb.set_trace()
#         if not isinstance(ary, SeriesType):
#             return
#         out = get_array_index_type(ary, idx)
#         # check result to be dt64 since it might be sliced array
#         # replace result with Timestamp
#         if out is not None and out.result == types.NPDatetime('ns'):
#             return signature(pandas_timestamp_type, ary, out.index)

def install_array_method(name, generic, support_literals=False):
    # taken from arraydecl.py, Series instead of Array
    my_attr = {"key": "array." + name, "generic": generic}
    temp_class = type("Series_" + name, (AbstractTemplate,), my_attr)
    if support_literals:
        temp_class.support_literals = support_literals
    def array_attribute_attachment(self, ary):
        return types.BoundFunction(temp_class, ary)

    setattr(SeriesAttribute, "resolve_" + name, array_attribute_attachment)

def generic_expand_cumulative_series(self, args, kws):
    # taken from arraydecl.py, replaced Array with Series
    assert not args
    assert not kws
    assert isinstance(self.this, SeriesType)
    return_type = SeriesType(dtype=_expand_integer(self.this.dtype),
                              ndim=1, layout='C')
    return signature(return_type, recvr=self.this)

# replacing cumsum/cumprod since arraydecl.py definition uses types.Array
for fname in ["cumsum", "cumprod"]:
    install_array_method(fname, generic_expand_cumulative_series)

# TODO: add itemsize, strides, etc. when removed from Pandas
_not_series_array_attrs = ['flat', 'ctypes', 'itemset', 'reshape', 'sort', 'flatten']

# use ArrayAttribute for attributes not defined in SeriesAttribute
for attr, func in numba.typing.arraydecl.ArrayAttribute.__dict__.items():
    if (attr.startswith('resolve_')
            and attr not in SeriesAttribute.__dict__
            and attr not in _not_series_array_attrs):
        setattr(SeriesAttribute, attr, func)

@infer
class GetItemSeries(AbstractTemplate):
    key = "getitem"

    def generic(self, args, kws):
        assert not kws
        [in_arr, in_idx] = args
        is_arr_series = False
        is_idx_series = False
        is_arr_dt_index = False

        if not isinstance(in_arr, SeriesType) and not isinstance(in_idx, SeriesType):
            return None

        if isinstance(in_arr, SeriesType):
            in_arr = series_to_array_type(in_arr)
            is_arr_series = True
            if in_arr.dtype == types.NPDatetime('ns'):
                is_arr_dt_index = True

        if isinstance(in_idx, SeriesType):
            in_idx = series_to_array_type(in_idx)
            is_idx_series = True

        # TODO: dt_index
        if in_arr == string_array_type:
            sig = GetItemStringArray.generic(self, (in_arr, in_idx), kws)
        else:
            out = get_array_index_type(in_arr, in_idx)
            sig = signature(out.result, in_arr, out.index)

        if sig is not None:
            arg1 = sig.args[0]
            arg2 = sig.args[1]
            if is_arr_series:
                sig.return_type = if_arr_to_series_type(sig.return_type)
                arg1 = if_arr_to_series_type(arg1)
            if is_idx_series:
                arg2 = if_arr_to_series_type(arg2)
            sig.args = (arg1, arg2)
            # dt_index and Series(dt64) should return Timestamp
            if is_arr_dt_index and sig.return_type == types.NPDatetime('ns'):
                sig.return_type = pandas_timestamp_type
        return sig

@infer
class SetItemSeries(SetItemBuffer):
    key = "setitem"

    def generic(self, args, kws):
        assert not kws
        series, idx, val = args
        if not isinstance(series, SeriesType):
            return None
        ary = series_to_array_type(series)
        # TODO: strings, dt_index
        res = super(SetItemSeries, self).generic((ary, idx, val), kws)
        if res is not None:
            new_series = if_arr_to_series_type(res.args[0])
            res.args = (new_series, res.args[1], res.args[2])
            return res

def series_op_generic(cls, self, args, kws):
    # return if no Series
    if not any(isinstance(arg, SeriesType) for arg in args):
        return None
    # convert args to array
    new_args = tuple(if_series_to_array_type(arg) for arg in args)
    sig = super(cls, self).generic(new_args, kws)
    # convert back to Series
    if sig is not None:
        sig.return_type = if_arr_to_series_type(sig.return_type)
        sig.args = tuple(if_arr_to_series_type(a) for a in sig.args)
    return sig

class SeriesOpUfuncs(NumpyRulesArrayOperator):
    def generic(self, args, kws):
        return series_op_generic(SeriesOpUfuncs, self, args, kws)

class SeriesInplaceOpUfuncs(NumpyRulesInplaceArrayOperator):
    def generic(self, args, kws):
        return series_op_generic(SeriesInplaceOpUfuncs, self, args, kws)

class SeriesUnaryOpUfuncs(NumpyRulesUnaryArrayOperator):
    def generic(self, args, kws):
        return series_op_generic(SeriesUnaryOpUfuncs, self, args, kws)

# TODO: change class name to Series in install_operations
SeriesOpUfuncs.install_operations()
SeriesInplaceOpUfuncs.install_operations()
SeriesUnaryOpUfuncs.install_operations()

class Series_Numpy_rules_ufunc(Numpy_rules_ufunc):
    def generic(self, args, kws):
        return series_op_generic(Series_Numpy_rules_ufunc, self, args, kws)

# copied from npydecl.py since deleted
_aliases = set(["bitwise_not", "mod", "abs"])
if np.divide == np.true_divide:
    _aliases.add("divide")

for func in numba.typing.npydecl.supported_ufuncs:
    name = func.__name__
    #_numpy_ufunc(func)
    class typing_class(Series_Numpy_rules_ufunc):
        key = func

    typing_class.__name__ = "resolve_series_{0}".format(name)

    if not name in _aliases:
        infer_global(func, types.Function(typing_class))

@infer_global(len)
class LenSeriesType(AbstractTemplate):
    def generic(self, args, kws):
        if not kws and len(args) == 1 and isinstance(args[0], SeriesType):
            return signature(types.intp, *args)

# @infer_global(np.empty_like)
# @infer_global(np.zeros_like)
# @infer_global(np.ones_like)
# class SeriesLikeTyper(NdConstructorLike):
#     def generic(self):
#         typer = super(SeriesLikeTyper, self).generic()
#         def wrapper(*args, **kws):
#             new_args = tuple(if_series_to_array_type(arg) for arg in args)
#             new_kws = {n:if_series_to_array_type(t) for n,t in kws.items()}
#             return typer(*new_args, **new_kws)
#         return wrapper

#@infer_global(np.full_like)
