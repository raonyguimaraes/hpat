import numpy as np
import numba
import hpat
from numba import types
from numba.typing.templates import (infer_global, AbstractTemplate, infer,
    signature, AttributeTemplate, infer_getattr, bound_function)
import numba.typing.typeof
from numba.extending import (typeof_impl, type_callable, models, register_model, NativeValue,
                             make_attribute_wrapper, lower_builtin, box, unbox,
                             lower_getattr, intrinsic, overload_method, overload, overload_attribute)
from numba import cgutils
from hpat.str_ext import string_type, del_str
from numba.targets.imputils import impl_ret_new_ref, impl_ret_borrowed, iternext_impl
import llvmlite.llvmpy.core as lc
from glob import glob

char_typ = types.uint8
offset_typ = types.uint32

data_ctypes_type = types.ArrayCTypes(types.Array(char_typ, 1, 'C'))
offset_ctypes_type = types.ArrayCTypes(types.Array(offset_typ, 1, 'C'))

class StringArray(object):
    def __init__(self, str_list):
        # dummy constructor
        self.num_strings = len(str_list)
        self.offsets = str_list
        self.data = str_list

    def __repr__(self):
        return 'StringArray({})'.format(self.data)


class StringArrayType(types.IterableType):
    def __init__(self):
        super(StringArrayType, self).__init__(
            name='StringArrayType()')

    @property
    def dtype(self):
        return string_type

    @property
    def iterator_type(self):
        return StringArrayIterator()


string_array_type = StringArrayType()


@typeof_impl.register(StringArray)
def typeof_string_array(val, c):
    return string_array_type

# @type_callable(StringArray)
# def type_string_array_call(context):
#     def typer(offset, data):
#         return string_array_type
#     return typer


@type_callable(StringArray)
def type_string_array_call2(context):
    def typer(string_list=None):
        return string_array_type
    return typer


class StringArrayPayloadType(types.Type):
    def __init__(self):
        super(StringArrayPayloadType, self).__init__(
            name='StringArrayPayloadType()')

str_arr_payload_type = StringArrayPayloadType()

@register_model(StringArrayPayloadType)
class StringArrayPayloadModel(models.StructModel):
    def __init__(self, dmm, fe_type):
        members = [
            ('offsets', types.CPointer(offset_typ)),
            ('data', types.CPointer(char_typ)),
        ]
        models.StructModel.__init__(self, dmm, fe_type, members)


@register_model(StringArrayType)
class StringArrayModel(models.StructModel):
    def __init__(self, dmm, fe_type):
        members = [
            ('num_items', types.uint64),
            ('num_total_chars', types.uint64),
            ('offsets', types.CPointer(offset_typ)),
            ('data', types.CPointer(char_typ)),
            ('meminfo', types.MemInfoPointer(str_arr_payload_type)),
        ]
        models.StructModel.__init__(self, dmm, fe_type, members)

# TODO: fix overload for things like 'getitem'
# @overload('getitem')
# def str_arr_getitem_bool_overload(str_arr_tp, bool_arr_tp):
#     import pdb; pdb.set_trace()
#     if str_arr_tp == string_array_type and bool_arr_tp == types.Array(types.bool_, 1, 'C'):
#         def str_arr_bool_impl(str_arr, bool_arr):
#             n = len(str_arr)
#             if n!=len(bool_arr):
#                 raise IndexError("boolean index did not match indexed array along dimension 0")
#             return str_arr
#         return str_arr_bool_impl

class StringArrayIterator(types.SimpleIteratorType):
    """
    Type class for iterators of string arrays.
    """

    def __init__(self):
        name = "iter(String)"
        yield_type = string_type
        super(StringArrayIterator, self).__init__(name, yield_type)

@register_model(StringArrayIterator)
class StrArrayIteratorModel(models.StructModel):
    def __init__(self, dmm, fe_type):
        # We use an unsigned index to avoid the cost of negative index tests.
        members = [('index', types.EphemeralPointer(types.uintp)),
                   ('array', string_array_type)]
        super(StrArrayIteratorModel, self).__init__(dmm, fe_type, members)

lower_builtin('getiter', string_array_type)(numba.targets.arrayobj.getiter_array)

@lower_builtin('iternext', StringArrayIterator)
@iternext_impl
def iternext_str_array(context, builder, sig, args, result):
    [iterty] = sig.args
    [iter_arg] = args

    iterobj = context.make_helper(builder, iterty, value=iter_arg)
    len_sig = signature(types.intp, string_array_type)
    nitems = context.compile_internal(builder, lambda a: len(a), len_sig, [iterobj.array])

    index = builder.load(iterobj.index)
    is_valid = builder.icmp(lc.ICMP_SLT, index, nitems)
    result.set_valid(is_valid)

    with builder.if_then(is_valid):
        getitem_sig = signature(string_type, string_array_type, types.intp)
        value = context.compile_internal(builder, lambda a,i: a[i], getitem_sig, [iterobj.array, index])
        result.yield_(value)
        nindex = cgutils.increment_index(builder, index)
        builder.store(nindex, iterobj.index)

@intrinsic
def num_total_chars(typingctx, str_arr_typ=None):
    # None default to make IntelliSense happy
    assert is_str_arr_typ(str_arr_typ)
    def codegen(context, builder, sig, args):
        in_str_arr, = args
        string_array = context.make_helper(builder, string_array_type, in_str_arr)
        return string_array.num_total_chars

    return types.uint64(string_array_type), codegen


@intrinsic
def get_offset_ptr(typingctx, str_arr_typ=None):
    assert is_str_arr_typ(str_arr_typ)
    def codegen(context, builder, sig, args):
        in_str_arr, = args

        string_array = context.make_helper(builder, string_array_type, in_str_arr)
        #return string_array.offsets
        # # Create new ArrayCType structure
        ctinfo = context.make_helper(builder, offset_ctypes_type)
        ctinfo.data = builder.bitcast(string_array.offsets, lir.IntType(32).as_pointer())
        ctinfo.meminfo = string_array.meminfo
        res = ctinfo._getvalue()
        return impl_ret_borrowed(context, builder, offset_ctypes_type, res)

    return offset_ctypes_type(string_array_type), codegen

@intrinsic
def get_data_ptr(typingctx, str_arr_typ=None):
    assert is_str_arr_typ(str_arr_typ)
    def codegen(context, builder, sig, args):
        in_str_arr, = args

        string_array = context.make_helper(builder, string_array_type, in_str_arr)
        #return string_array.data
        # Create new ArrayCType structure
        # TODO: put offset/data in main structure since immutable
        ctinfo = context.make_helper(builder, data_ctypes_type)
        ctinfo.data = string_array.data
        ctinfo.meminfo = string_array.meminfo
        res = ctinfo._getvalue()
        return impl_ret_borrowed(context, builder, data_ctypes_type, res)

    return data_ctypes_type(string_array_type), codegen

@intrinsic
def getitem_str_offset(typingctx, str_arr_typ, ind_t):
    def codegen(context, builder, sig, args):
        in_str_arr, ind = args

        string_array = context.make_helper(builder, string_array_type, in_str_arr)
        offsets = builder.bitcast(string_array.offsets, lir.IntType(32).as_pointer())
        return builder.load(builder.gep(offsets, [ind]))

    return types.uint32(string_array_type, ind_t), codegen

@intrinsic
def copy_str_arr_slice(typingctx, str_arr_typ, out_str_arr_typ, ind_t=None):
    def codegen(context, builder, sig, args):
        out_str_arr, in_str_arr, ind = args

        in_string_array = context.make_helper(builder, string_array_type, in_str_arr)

        out_string_array = context.make_helper(builder, string_array_type, out_str_arr)

        in_offsets = builder.bitcast(in_string_array.offsets, lir.IntType(32).as_pointer())
        out_offsets = builder.bitcast(out_string_array.offsets, lir.IntType(32).as_pointer())

        ind_p1 = builder.add(ind, context.get_constant(types.intp, 1))
        cgutils.memcpy(builder, out_offsets, in_offsets, ind_p1)
        cgutils.memcpy(builder, out_string_array.data, in_string_array.data, builder.load(builder.gep(in_offsets, [ind])))
        return context.get_dummy_value()

    return types.void(string_array_type, string_array_type, ind_t), codegen


# convert array to list of strings if it is StringArray
# just return it otherwise
def to_string_list(arr):
    return arr

@overload(to_string_list)
def to_string_list_overload(arr_typ):
    if is_str_arr_typ(arr_typ):
        def to_string_impl(str_arr):
            n = len(str_arr)
            l_str = []
            for i in range(n):
                l_str.append(str_arr[i])
            return l_str
        return to_string_impl

    if isinstance(arr_typ, (types.Tuple, types.UniTuple)):
        count = arr_typ.count

        func_text = "def f(data):\n"
        func_text += "  return ({}{})\n".format(','.join(["to_string_list(data[{}])".format(
            i) for i in range(count)]),
            "," if count == 1 else "")  # single value needs comma to become tuple

        loc_vars = {}
        exec(func_text, {'to_string_list': to_string_list}, loc_vars)
        to_str_impl = loc_vars['f']
        return to_str_impl

    return lambda a: a

def cp_str_list_to_array(str_arr, str_list):
    return

@overload(cp_str_list_to_array)
def cp_str_list_to_array_overload(arr_typ, list_typ):
    if is_str_arr_typ(arr_typ):
        def cp_str_list_impl(str_arr, str_list):
            n = len(str_list)
            for i in range(n):
                _str = str_list[i]
                setitem_string_array(get_offset_ptr(str_arr), get_data_ptr(str_arr), _str, i)
                del_str(_str)

        return cp_str_list_impl

    if isinstance(arr_typ, (types.Tuple, types.UniTuple)):
        count = arr_typ.count

        func_text = "def f(data, l_data):\n"
        for i in range(count):
            func_text += "  cp_str_list_to_array(data[{}], l_data[{}])\n".format(i, i)
        func_text += "  return\n"

        loc_vars = {}
        exec(func_text, {'cp_str_list_to_array': cp_str_list_to_array}, loc_vars)
        cp_str_impl = loc_vars['f']
        return cp_str_impl

    return lambda a,b: None


def str_list_to_array(str_list):
    return str_list

@overload(str_list_to_array)
def str_list_to_array_overload(list_typ):
    if list_typ == types.List(string_type):
        def str_list_impl(str_list):
            n = len(str_list)
            n_char = 0
            for i in range(n):
                _str = str_list[i]
                n_char += len(_str)
            str_arr = pre_alloc_string_array(n, n_char)
            for i in range(n):
                _str = str_list[i]
                setitem_string_array(get_offset_ptr(str_arr), get_data_ptr(str_arr), _str, i)
                del_str(_str)  # XXX assuming str list is not used anymore
            return str_arr

        return str_list_impl

    return lambda a: a

@infer
class GetItemStringArray(AbstractTemplate):
    key = "getitem"

    def generic(self, args, kws):
        assert not kws
        [ary, idx] = args
        if isinstance(ary, StringArrayType):
            if isinstance(idx, types.SliceType):
                return signature(string_array_type, *args)
            elif isinstance(idx, types.Integer):
                return signature(string_type, *args)
            elif idx == types.Array(types.bool_, 1, 'C'):
                return signature(string_array_type, *args)
            elif idx == types.Array(types.intp, 1, 'C'):
                return signature(string_array_type, *args)


@infer
class CmpOpEqStringArray(AbstractTemplate):
    key = '=='

    def generic(self, args, kws):
        assert not kws
        [va, vb] = args
        # if one of the inputs is string array
        if va == string_array_type or vb == string_array_type:
            # inputs should be either string array or string
            assert is_str_arr_typ(va) or va == string_type
            assert is_str_arr_typ(vb) or vb == string_type
            return signature(types.Array(types.boolean, 1, 'C'), va, vb)


@infer
class CmpOpNEqStringArray(CmpOpEqStringArray):
    key = '!='

@infer
class CmpOpGEStringArray(CmpOpEqStringArray):
    key = '>='

@infer
class CmpOpGTStringArray(CmpOpEqStringArray):
    key = '>'

@infer
class CmpOpLEStringArray(CmpOpEqStringArray):
    key = '<='

@infer
class CmpOpLTStringArray(CmpOpEqStringArray):
    key = '<'

from hpat.pd_series_ext import string_series_type

def is_str_arr_typ(typ):
    return typ == string_array_type or typ == string_series_type

# @infer_global(len)
# class LenStringArray(AbstractTemplate):
#     def generic(self, args, kws):
#         if not kws and len(args)==1 and args[0]==string_array_type:
#             return signature(types.intp, *args)

# XXX: should these be exposed?
# make_attribute_wrapper(StringArrayType, 'num_items', 'num_items')
# make_attribute_wrapper(StringArrayType, 'num_total_chars', 'num_total_chars')
# make_attribute_wrapper(StringArrayType, 'offsets', 'offsets')
# make_attribute_wrapper(StringArrayType, 'data', 'data')

# make_attribute_wrapper(StringArrayPayloadType, 'offsets', 'offsets')
# make_attribute_wrapper(StringArrayPayloadType, 'data', 'data')

# XXX can't use this with overload_method
@infer_getattr
class StrArrayAttribute(AttributeTemplate):
    key = string_array_type

    def resolve_size(self, ctflags):
        return types.intp

    @bound_function("str_arr.copy")
    def resolve_copy(self, ary, args, kws):
        return signature(string_array_type, *args)

@lower_builtin("str_arr.copy", string_array_type)
def str_arr_copy_impl(context, builder, sig, args):
    return context.compile_internal(builder, copy_impl, sig, args)


def copy_impl(arr):
    n = len(arr)
    n_chars = num_total_chars(arr)
    new_arr = pre_alloc_string_array(n, np.int64(n_chars))
    copy_str_arr_slice(new_arr, arr, n)
    return new_arr

# @overload_method(StringArrayType, 'copy')
# def string_array_copy(arr_t):
#     return copy_impl


# @overload_attribute(string_array_type, 'size')
# def string_array_attr_size(arr_t):
#     return get_str_arr_size

# def get_str_arr_size(arr):  # pragma: no cover
#     return len(arr)

# @infer_global(get_str_arr_size)
# class StrArrSizeInfer(AbstractTemplate):
#     def generic(self, args, kws):
#         assert not kws
#         assert len(args) == 1 and args[0] == string_array_type
#         return signature(types.intp, *args)

# @lower_builtin(get_str_arr_size, string_array_type)
# def str_arr_size_impl(context, builder, sig, args):

@lower_getattr(string_array_type, 'size')
def str_arr_size_impl(context, builder, typ, val):
    string_array = context.make_helper(builder, string_array_type, val)

    attrval = string_array.num_items
    attrty = types.intp
    return impl_ret_borrowed(context, builder, attrty, attrval)

# @lower_builtin(StringArray, types.Type, types.Type)
# def impl_string_array(context, builder, sig, args):
#     typ = sig.return_type
#     offsets, data = args
#     string_array = cgutils.create_struct_proxy(typ)(context, builder)
#     string_array.offsets = offsets
#     string_array.data = data
#     return string_array._getvalue()



@overload(len)
def str_arr_len_overload(str_arr):
    if is_str_arr_typ(str_arr):
        def str_arr_len(s):
            return s.size
        return str_arr_len


from numba.targets.listobj import ListInstance
from llvmlite import ir as lir
import llvmlite.binding as ll
import hstr_ext
ll.add_symbol('get_str_len', hstr_ext.get_str_len)
ll.add_symbol('allocate_string_array', hstr_ext.allocate_string_array)
ll.add_symbol('setitem_string_array', hstr_ext.setitem_string_array)
ll.add_symbol('getitem_string_array', hstr_ext.getitem_string_array)
ll.add_symbol('getitem_string_array_std', hstr_ext.getitem_string_array_std)
ll.add_symbol('string_array_from_sequence', hstr_ext.string_array_from_sequence)
ll.add_symbol('np_array_from_string_array', hstr_ext.np_array_from_string_array)
ll.add_symbol('print_int', hstr_ext.print_int)
ll.add_symbol('convert_len_arr_to_offset', hstr_ext.convert_len_arr_to_offset)
ll.add_symbol('set_string_array_range', hstr_ext.set_string_array_range)

convert_len_arr_to_offset = types.ExternalFunction("convert_len_arr_to_offset", types.void(types.voidptr, types.intp))

import hstr_ext
ll.add_symbol('dtor_string_array', hstr_ext.dtor_string_array)
ll.add_symbol('c_glob', hstr_ext.c_glob)

setitem_string_array = types.ExternalFunction("setitem_string_array",
            types.void(types.voidptr, types.voidptr, string_type, types.intp))

def construct_string_array(context, builder):
    """Creates meminfo and sets dtor.
    """
    alloc_type = context.get_data_type(str_arr_payload_type)
    alloc_size = context.get_abi_sizeof(alloc_type)

    llvoidptr = context.get_value_type(types.voidptr)
    llsize = context.get_value_type(types.uintp)
    dtor_ftype = lir.FunctionType(lir.VoidType(),
                                  [llvoidptr, llsize, llvoidptr])
    dtor_fn = builder.module.get_or_insert_function(
        dtor_ftype, name="dtor_string_array")

    meminfo = context.nrt.meminfo_alloc_dtor(
        builder,
        context.get_constant(types.uintp, alloc_size),
        dtor_fn,
    )
    meminfo_data_ptr = context.nrt.meminfo_data(builder, meminfo)
    meminfo_data_ptr = builder.bitcast(meminfo_data_ptr,
                                   alloc_type.as_pointer())

    # Nullify all data
    # builder.store( cgutils.get_null_value(alloc_type),
    #             meminfo_data_ptr)
    return meminfo, meminfo_data_ptr


@lower_builtin(StringArray)
@lower_builtin(StringArray, types.List)
def impl_string_array_single(context, builder, sig, args):
    typ = sig.return_type
    zero = context.get_constant(types.intp, 0)
    meminfo, meminfo_data_ptr = construct_string_array(context, builder)

    str_arr_payload = cgutils.create_struct_proxy(str_arr_payload_type)(context, builder)
    if not sig.args:  # return empty string array if no args
        # XXX alloc empty arrays for dtor to safely delete?
        builder.store(str_arr_payload._getvalue(), meminfo_data_ptr)
        string_array = context.make_helper(builder, typ)
        string_array.meminfo = meminfo
        string_array.num_items = zero
        string_array.num_total_chars = zero
        ret = string_array._getvalue()
        #context.nrt.decref(builder, ty, ret)

        return impl_ret_new_ref(context, builder, typ, ret)

    string_list = ListInstance(context, builder, sig.args[0], args[0])

    # get total size of string buffer
    fnty = lir.FunctionType(lir.IntType(64),
                            [lir.IntType(8).as_pointer()])
    fn_len = builder.module.get_or_insert_function(fnty, name="get_str_len")
    total_size = cgutils.alloca_once_value(builder, zero)

    # loop through all strings and get length
    with cgutils.for_range(builder, string_list.size) as loop:
        str_value = string_list.getitem(loop.index)
        str_len = builder.call(fn_len, [str_value])
        builder.store(builder.add(builder.load(total_size), str_len), total_size)

    # allocate string array
    fnty = lir.FunctionType(lir.VoidType(),
                            [lir.IntType(32).as_pointer().as_pointer(),
                             lir.IntType(8).as_pointer().as_pointer(),
                             lir.IntType(64),
                             lir.IntType(64)])
    fn_alloc = builder.module.get_or_insert_function(fnty,
                                                     name="allocate_string_array")
    builder.call(fn_alloc, [str_arr_payload._get_ptr_by_name('offsets'),
                            str_arr_payload._get_ptr_by_name('data'),
                            string_list.size, builder.load(total_size)])

    # set string array values
    fnty = lir.FunctionType(lir.VoidType(),
                            [lir.IntType(32).as_pointer(),
                             lir.IntType(8).as_pointer(),
                             lir.IntType(8).as_pointer(),
                             lir.IntType(64)])
    fn_setitem = builder.module.get_or_insert_function(fnty,
                                                       name="setitem_string_array")

    with cgutils.for_range(builder, string_list.size) as loop:
        str_value = string_list.getitem(loop.index)
        builder.call(fn_setitem, [str_arr_payload.offsets, str_arr_payload.data,
                                  str_value, loop.index])

    builder.store(str_arr_payload._getvalue(), meminfo_data_ptr)

    string_array = context.make_helper(builder, typ)
    string_array.num_items = string_list.size
    string_array.num_total_chars = builder.load(total_size)
    #cgutils.printf(builder, "str %d %d\n", string_array.num_items, string_array.num_total_chars)
    string_array.offsets = str_arr_payload.offsets
    string_array.data = str_arr_payload.data
    string_array.meminfo = meminfo
    ret = string_array._getvalue()
    #context.nrt.decref(builder, ty, ret)

    return impl_ret_new_ref(context, builder, typ, ret)

@intrinsic
def pre_alloc_string_array(typingctx, num_strs_typ, num_total_chars_typ=None):
    assert num_strs_typ == types.intp and num_total_chars_typ == types.intp
    def codegen(context, builder, sig, args):
        num_strs, num_total_chars = args
        meminfo, meminfo_data_ptr = construct_string_array(context, builder)

        str_arr_payload = cgutils.create_struct_proxy(str_arr_payload_type)(context, builder)

        # allocate string array
        fnty = lir.FunctionType(lir.VoidType(),
                                [lir.IntType(32).as_pointer().as_pointer(),
                                 lir.IntType(8).as_pointer().as_pointer(),
                                 lir.IntType(64),
                                 lir.IntType(64)])
        fn_alloc = builder.module.get_or_insert_function(fnty,
                                                         name="allocate_string_array")
        builder.call(fn_alloc, [str_arr_payload._get_ptr_by_name('offsets'),
                                str_arr_payload._get_ptr_by_name('data'),
                                num_strs,
                                num_total_chars])

        builder.store(str_arr_payload._getvalue(), meminfo_data_ptr)
        string_array = context.make_helper(builder, string_array_type)
        string_array.num_items = num_strs
        string_array.num_total_chars = num_total_chars
        string_array.offsets = str_arr_payload.offsets
        string_array.data = str_arr_payload.data
        string_array.meminfo = meminfo
        ret = string_array._getvalue()
        #context.nrt.decref(builder, ty, ret)

        return impl_ret_new_ref(context, builder, string_array_type, ret)

    return string_array_type(types.intp, types.intp), codegen


@intrinsic
def set_string_array_range(typingctx, out_typ, in_typ, curr_str_typ, curr_chars_typ=None):
    assert is_str_arr_typ(out_typ) and is_str_arr_typ(in_typ)
    assert curr_str_typ == types.intp and curr_chars_typ == types.intp
    def codegen(context, builder, sig, args):
        out_arr, in_arr, curr_str_ind, curr_chars_ind = args

        # get input/output struct
        out_string_array = context.make_helper(builder, string_array_type, out_arr)
        in_string_array = context.make_helper(builder, string_array_type, in_arr)

        fnty = lir.FunctionType(lir.VoidType(),
                                [lir.IntType(32).as_pointer(),
                                 lir.IntType(8).as_pointer(),
                                 lir.IntType(32).as_pointer(),
                                 lir.IntType(8).as_pointer(),
                                 lir.IntType(64),
                                 lir.IntType(64),
                                 lir.IntType(64),
                                 lir.IntType(64),])
        fn_alloc = builder.module.get_or_insert_function(fnty,
                                                         name="set_string_array_range")
        builder.call(fn_alloc, [out_string_array.offsets,
                                out_string_array.data,
                                in_string_array.offsets,
                                in_string_array.data,
                                curr_str_ind,
                                curr_chars_ind,
                                in_string_array.num_items,
                                in_string_array.num_total_chars,
                                ])

        return context.get_dummy_value()

    return types.void(string_array_type, string_array_type, types.intp, types.intp), codegen

@box(StringArrayType)
def box_str_arr(typ, val, c):
    """
    """

    string_array = c.context.make_helper(c.builder, typ, val)

    fnty = lir.FunctionType(c.context.get_argument_type(types.pyobject), #lir.IntType(8).as_pointer(),
                            [lir.IntType(64),
                             lir.IntType(32).as_pointer(),
                             lir.IntType(8).as_pointer()])
    fn_get = c.builder.module.get_or_insert_function(fnty, name="np_array_from_string_array")

    arr = c.builder.call(fn_get, [string_array.num_items, string_array.offsets, string_array.data])

    c.context.nrt.decref(c.builder, typ, val)
    return arr #c.builder.load(arr)


@lower_builtin('getitem', StringArrayType, types.Integer)
def lower_string_arr_getitem(context, builder, sig, args):
    typ = sig.args[0]

    string_array = context.make_helper(builder, typ, args[0])

    fnty = lir.FunctionType(lir.IntType(8).as_pointer(),
                            [lir.IntType(32).as_pointer(),
                             lir.IntType(8).as_pointer(),
                             lir.IntType(64)])
    fn_getitem = builder.module.get_or_insert_function(fnty,
                                                       name="getitem_string_array_std")
    return builder.call(fn_getitem, [string_array.offsets,
                                     string_array.data, args[1]])


@lower_builtin('getitem', StringArrayType, types.Array(types.bool_, 1, 'C'))
def lower_string_arr_getitem_bool(context, builder, sig, args):
    def str_arr_bool_impl(str_arr, bool_arr):
        n = len(str_arr)
        if n != len(bool_arr):
            raise IndexError("boolean index did not match indexed array along dimension 0")
        n_strs = 0
        n_chars = 0
        for i in range(n):
            if bool_arr[i] == True:
                # TODO: use get_cstr_and_len instead of getitem
                str = str_arr[i]
                n_strs += 1
                n_chars += len(str)
                del_str(str)
        out_arr = pre_alloc_string_array(n_strs, n_chars)
        str_ind = 0
        for i in range(n):
            if bool_arr[i] == True:
                str = str_arr[i]
                setitem_string_array(get_offset_ptr(out_arr), get_data_ptr(out_arr), str, str_ind)
                str_ind += 1
                del_str(str)
        return out_arr
    res = context.compile_internal(builder, str_arr_bool_impl, sig, args)
    return res


@lower_builtin('getitem', StringArrayType, types.Array(types.intp, 1, 'C'))
def lower_string_arr_getitem_arr(context, builder, sig, args):
    def str_arr_arr_impl(str_arr, ind_arr):
        n = len(ind_arr)
        # get lengths
        n_strs = 0
        n_chars = 0
        for i in range(n):
            # TODO: use get_cstr_and_len instead of getitem
            _str = str_arr[ind_arr[i]]
            n_strs += 1
            n_chars += len(_str)
            del_str(_str)

        out_arr = pre_alloc_string_array(n_strs, n_chars)
        str_ind = 0
        for i in range(n):
            _str = str_arr[ind_arr[i]]
            setitem_string_array(get_offset_ptr(out_arr), get_data_ptr(out_arr), _str, str_ind)
            str_ind += 1
            del_str(_str)
        return out_arr
    res = context.compile_internal(builder, str_arr_arr_impl, sig, args)
    return res


@typeof_impl.register(np.ndarray)
def typeof_np_string(val, c):
    if val.ndim == 1 and isinstance(val[0], str):  # and isinstance(val[-1], str):
        return string_array_type
    else:
        return numba.typing.typeof._typeof_ndarray(val, c)


@unbox(StringArrayType)
def unbox_str_series(typ, val, c):
    """
    Unbox a Pandas String Series. We just redirect to StringArray implementation.
    """
    dtype = StringArrayPayloadType()
    payload = cgutils.create_struct_proxy(dtype)(c.context, c.builder)
    string_array = c.context.make_helper(c.builder, typ)

    # function signature of string_array_from_sequence
    # we use void* instead of PyObject*
    fnty = lir.FunctionType(lir.VoidType(),
                            [lir.IntType(8).as_pointer(),
                             lir.IntType(64).as_pointer(),
                             lir.IntType(32).as_pointer().as_pointer(),
                             lir.IntType(8).as_pointer().as_pointer(),])
    fn = c.builder.module.get_or_insert_function(fnty, name="string_array_from_sequence")
    c.builder.call(fn, [val,
                        string_array._get_ptr_by_name('num_items'),
                        payload._get_ptr_by_name('offsets'),
                        payload._get_ptr_by_name('data'),])

    # the raw data is now copied to payload
    # The native representation is a proxy to the payload, we need to
    # get a proxy and attach the payload and meminfo
    meminfo, meminfo_data_ptr = construct_string_array(c.context, c.builder)
    c.builder.store(payload._getvalue(), meminfo_data_ptr)

    string_array.meminfo = meminfo
    string_array.offsets = payload.offsets
    string_array.data = payload.data
    string_array.num_total_chars = c.builder.zext(c.builder.load(
        c.builder.gep(string_array.offsets, [string_array.num_items])), lir.IntType(64))

    # FIXME how to check that the returned size is > 0?
    is_error = cgutils.is_not_null(c.builder, c.pyapi.err_occurred())
    return NativeValue(string_array._getvalue(), is_error=is_error)

# zero = context.get_constant(types.intp, 0)
# cond = builder.icmp_signed('>=', size, zero)
# with cgutils.if_unlikely(builder, cond):
# http://llvmlite.readthedocs.io/en/latest/user-guide/ir/ir-builder.html#comparisons


#### glob support #####

@infer_global(glob)
class GlobInfer(AbstractTemplate):
    def generic(self, args, kws):
        if not kws and len(args)==1 and args[0]==string_type:
            return signature(string_array_type, *args)


@lower_builtin(glob, string_type)
def lower_glob(context, builder, sig, args):
    path = args[0]
    typ = sig.return_type
    dtype = StringArrayPayloadType()
    meminfo, meminfo_data_ptr = construct_string_array(context, builder)
    string_array = context.make_helper(builder, typ)
    str_arr_payload = cgutils.create_struct_proxy(dtype)(context, builder)

    # call glob in C
    fnty = lir.FunctionType(lir.VoidType(),
                            [lir.IntType(32).as_pointer().as_pointer(),
                             lir.IntType(8).as_pointer().as_pointer(),
                             lir.IntType(64).as_pointer(),
                             lir.IntType(8).as_pointer()])
    fn = builder.module.get_or_insert_function(fnty, name="c_glob")
    builder.call(fn, [str_arr_payload._get_ptr_by_name('offsets'),
                            str_arr_payload._get_ptr_by_name('data'),
                            string_array._get_ptr_by_name('num_items'),
                            path])

    builder.store(str_arr_payload._getvalue(), meminfo_data_ptr)

    string_array.meminfo = meminfo
    string_array.offsets = str_arr_payload.offsets
    string_array.data = str_arr_payload.data
    string_array.num_total_chars = builder.zext(builder.load(
        builder.gep(string_array.offsets, [string_array.num_items])), lir.IntType(64))

    ret = string_array._getvalue()
    #context.nrt.decref(builder, ty, ret)

    return impl_ret_new_ref(context, builder, typ, ret)
