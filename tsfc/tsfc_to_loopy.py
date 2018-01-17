from __future__ import division, absolute_import, print_function

from functools import partial, singledispatch
import numpy as np
import six

import islpy as isl
import loopy as lp

import gem.gem as g
import pymbolic.primitives as p

from pytools import UniqueNameGenerator


# {{{ conversion context

class ConversionContext(object):
    def __init__(self, expr_use_count):
        self.expr_use_count = expr_use_count

        self.name_gen = UniqueNameGenerator()
        self.index_to_iname_and_length = {}
        self.var_to_name_and_shape = {}
        self.literal_to_name_and_array = {}
        self.node_to_var_name = {}
        self.node_to_inames = {}
        self.assignments = []
        self.subst_rules = []
        self.cse_names = set()

    def variable_to_name(self, node):
        # deals with both Variable and VariableIndex nodes
        try:
            name, shape = self.var_to_name_and_shape[node]
        except KeyError:
            name = self.name_gen(node.name)
            self.var_to_name_and_shape[node] = (name, node.shape)

        else:
            assert node.shape == shape

        return name

    def literal_to_name(self, literal):
        try:
            name, array = self.literal_to_name_and_array[literal]
        except KeyError:
            name = self.name_gen("cnst")
            self.literal_to_name_and_array[literal] = (name, literal.array)

        else:
            assert np.array_equal(array, literal.array)

        return name

    def index_to_iname(self, index):
        try:
            iname, length = self.index_to_iname_and_length[index]
        except KeyError:
            if index.name is None:
                iname = self.name_gen("i%d" % index.count)
            else:
                iname = self.name_gen(index.name)

            self.index_to_iname_and_length[index] = (iname, index.extent)

        else:
            assert index.extent == length

        return iname

    def _is_cse_eligible(self, node):
        if not (isinstance(node, g.Literal) and node.array.shape == ()):

            if isinstance(node, (g.FlexiblyIndexed, g.Indexed)):
                return False
            else:
                return True
        else:
            return False

        return not (
            (isinstance(node, g.Literal) and node.array.shape == ()) or

            not isinstance(node, g.FlexiblyIndexed))

    def rec_gem(self, node, parent):
        if (
                self.expr_use_count.get(node, 0) > 1 and
                self._is_cse_eligible(node)):
            try:
                var_name = self.node_to_var_name[node]
                free_inames = self.node_to_inames[node]
            except KeyError:
                result = expr_to_loopy(node, self)
                var_name = self.name_gen("cse")
                self.cse_names.add(var_name)
                self.node_to_var_name[node] = var_name
                free_inames = tuple(
                    self.index_to_iname(i) for i in node.free_indices)
                self.node_to_inames[node] = node.free_indices

                self.assignments.append((var_name, free_inames, result))

            if len(free_inames) == 0:
                return p.Variable(var_name)
            else:
                return p.Subscript(p.Variable(var_name),
                                   tuple(index_to_loopy(i, self) for i in
                                         self.node_to_inames[node]))

        else:
            return expr_to_loopy(node, self)

# }}}


# {{{ index conversion

@singledispatch
def index_to_loopy(node, ctx):
    raise NotImplementedError(
        "ran into index type '%s', no conversion known"
        % type(node).__name__)


@index_to_loopy.register(g.Index)
def map_index(node, ctx):
    return p.Variable(ctx.index_to_iname(node))


@index_to_loopy.register(g.VariableIndex)
def map_varindex(node, ctx):
    return ctx.rec_gem(node.expression, None)


@index_to_loopy.register(int)
def map_int(node, ctx):
    return node

# }}}


# {{{ expression conversion

@singledispatch
def expr_to_loopy(node, ctx):
    raise NotImplementedError(
        "ran into node type '%s', no conversion known"
        % type(node).__name__)


@expr_to_loopy.register(g.Identity)
def map_identity(node, ctx):
    # no clear mapping of vectorial quantity into loopy
    raise NotImplementedError(type(node).__name__)


@expr_to_loopy.register(g.Literal)
def map_literal(node, ctx):
    if node.array.shape == ():
        return node.array[()]
    else:
        return p.Variable(g.literal_to_name(node))


@expr_to_loopy.register(g.Zero)
def map_zero(node, ctx):
    # no clear mapping of vectorial quantity into loopy
    raise NotImplementedError(type(node).__name__)


@expr_to_loopy.register(g.Variable)
def map_variable(node, ctx):
    return p.Variable(ctx.variable_to_name(node))


def convert_multichild(pymbolic_cls, node, ctx):
    return pymbolic_cls(tuple(ctx.rec_gem(c, node) for c in node.children))


expr_to_loopy.register(g.Sum)(partial(convert_multichild, p.Sum))
expr_to_loopy.register(g.Product)(
    partial(convert_multichild, p.Product))


@expr_to_loopy.register(g.Division)
def _(node, ctx):
    num, denom = node.children
    return p.Quotient(ctx.rec_gem(num, node), ctx.rec_gem(denom, node))


@expr_to_loopy.register(g.Power)
def map_power(node, ctx):
    base, exponent = node.children
    return p.Power(ctx.rec_gem(base, node), ctx.rec_gem(exponent, node))


@expr_to_loopy.register(g.MathFunction)
def map_function(node, ctx):
    return p.Variable(node.name)(
        *tuple(ctx.rec_gem(c, node) for c in node.children))


expr_to_loopy.register(g.MinValue)(partial(convert_multichild, p.Min))
expr_to_loopy.register(g.MaxValue)(partial(convert_multichild, p.Max))


@expr_to_loopy.register(g.Comparison)
def map_comparison(node, ctx):
    left, right = node.children
    return p.Comparison(
        ctx.rec_gem(left, node),
        node.operator,
        ctx.rec_gem(right, node))


def index_aggregate_to_name(c, ctx):
    if isinstance(c, g.Variable):
        return ctx.variable_to_name(c)

    elif isinstance(c, g.Constant):
        return ctx.literal_to_name(c)

    else:
        raise NotImplementedError(
            "indexing into %s" % type(c).__name__)


@expr_to_loopy.register(g.Indexed)
def map_indexed(node, ctx):
    c, = node.children

    return p.Subscript(
        p.Variable(index_aggregate_to_name(c, ctx)),
        tuple(index_to_loopy(i, ctx) for i in node.multiindex))


def cumulative_strides(strides):
    """Calculate cumulative strides from per-dimension capacities.

    For example:

        [2, 3, 4] ==> [12, 4, 1]

    """
    temp = np.flipud(np.cumprod(np.flipud(list(strides)[1:])))
    return tuple(temp) + (1,)


@expr_to_loopy.register(g.FlexiblyIndexed)
def map_flexibly_indexed(node, ctx):
    c, = node.children

    def flex_idx_to_loopy(f):
        off, idxs = f

        result = off
        for i, s in idxs:
            result += index_to_loopy(i, ctx)*s

        return result

    if c.shape == (None, ):
        return p.Variable(index_aggregate_to_name(c, ctx))

    return p.Subscript(
        p.Variable(index_aggregate_to_name(c, ctx)),
        tuple(flex_idx_to_loopy(i) for i in node.dim2idxs))


@expr_to_loopy.register(g.IndexSum)
def map_index_sum(node, ctx):
    c, = node.children

    subexpr = ctx.rec_gem(c, None)

    name = ctx.name_gen("sum_tmp")
    arg_names = tuple(
        ctx.index_to_iname(fi)
        for fi in node.free_indices)

    # new_arg_names = tuple(ctx.name_gen(an) for an in arg_names)

    # from pymbolic import substitute
    # subexpr = substitute(
    #     subexpr,
    #     dict(
    #         (an, p.Variable(nan))
    #         for an, nan in zip(arg_names, new_arg_names)))

    ctx.subst_rules.append(
        lp.SubstitutionRule(
            name,
            arg_names,
            lp.Reduction(
                "sum",
                tuple(ctx.index_to_iname(index) for index in node.multiindex),
                subexpr)))

    return p.Variable(name)(*tuple(p.Variable(n) for n in arg_names))

# }}}


# {{{ utilities

def count_subexpression_uses(node, expr_use_count):
    expr_use_count[node] = expr_use_count.get(node, 0) + 1
    for c in node.children:
        count_subexpression_uses(c, expr_use_count)


def get_empty_assumptions_domain(domain):
    dim_type = isl.dim_type

    dom_space = domain.get_space()
    assumptions_space = isl.Space.params_alloc(
        dom_space.get_ctx(), dom_space.dim(dim_type.param))
    for i in range(dom_space.dim(dim_type.param)):
        assumptions_space = assumptions_space.set_dim_name(
            dim_type.param, i,
            dom_space.get_dim_name(dim_type.param, i))
    return isl.BasicSet.universe(assumptions_space)

# }}}


# {{{ main entrypoint

def tsfc_to_loopy(ir, argument_ordering, kernel_name="tsfc_kernel",
        generate_increments=False):
    new_argument_ordering = []
    for idx in argument_ordering:
        if idx not in new_argument_ordering:
            new_argument_ordering.append(idx)

    argument_ordering = new_argument_ordering
    del new_argument_ordering

    expr_use_count = {}
    for lhs, expr in ir:
        count_subexpression_uses(expr, expr_use_count)

    ctx = ConversionContext(expr_use_count)

    exprs_and_free_inames = [
        (lhs, ctx.rec_gem(node, None),
            tuple(
                ctx.index_to_iname(i)
                for i in argument_ordering if i in node.free_indices))
        for lhs, node in ir]

    def subscr(name, indices):
        return (
            p.Variable(name)[
                tuple(p.Variable(i) for i in indices)]
            if indices else
            p.Variable(name))

    # {{{ instructions resulting from common subexpressions
    instructions = [
        lp.Assignment(
            subscr(var_name, free_indices),
            rhs,
            forced_iname_deps=frozenset(free_indices),
            forced_iname_deps_is_final=True,
            tags=frozenset(['cse']))
        for var_name, free_indices, rhs in ctx.assignments]

    # }}}

    pymbolic_lhss = []

    # {{{ instructions from IR

    for lhs, rhs, free_indices in exprs_and_free_inames:
        lhs_expr = ctx.rec_gem(lhs, None)
        pymbolic_lhss.append(lhs_expr)

        assert isinstance(lhs_expr, p.Subscript)
        from pymbolic.mapper.dependency import DependencyMapper
        iname_deps = list(DependencyMapper(composite_leaves=False)(lhs_expr.index_tuple))

        for iname_dep in iname_deps:
            assert isinstance(iname_dep, p.Variable)

            lhs_expr_single = p.Variable(ctx.name_gen(lhs_expr.aggregate.name))[iname_dep]

            if generate_increments:
                assignment_rhs = lhs_expr + rhs
            else:
                assignment_rhs = rhs

            instructions.append(lp.Assignment(
                lhs_expr_single,
                assignment_rhs,
                forced_iname_deps=frozenset(free_indices),
                forced_iname_deps_is_final=True))

    # }}}

    # {{{ construct domain

    inames = isl.make_zero_and_vars([
        iname
        for iname, length in six.itervalues(ctx.index_to_iname_and_length)])

    domain = None
    for iname, length in six.itervalues(ctx.index_to_iname_and_length):
        axis = (
            (inames[0].le_set(inames[iname])) &
            (inames[iname].lt_set(inames[0] + length)))

        if domain is None:
            domain = axis
        else:
            domain = domain & axis

    domain = domain.get_basic_sets()[0]

    # }}}

    # {{{ check disjointness of write footprints

    from loopy.symbolic import get_access_range

    assumptions = get_empty_assumptions_domain(domain)

    write_ranges = []
    for i, lhs in enumerate(pymbolic_lhss):
        write_range = get_access_range(domain, lhs.index_tuple, assumptions)

        for other_write_range in write_ranges:
            if not (write_range & other_write_range).is_empty():
                raise ValueError("assignment write ranges are not disjoint")

    # }}}

    global_temps = [
        lp.TemporaryVariable(
            name, shape=lp.auto, initializer=val,
            scope=lp.temp_var_scope.GLOBAL,
            read_only=True)
        for name, val in six.itervalues(ctx.literal_to_name_and_array)]
    cse_temps = [
        lp.TemporaryVariable(
            name, dtype=np.float64, shape=lp.auto, base_indices=lp.auto,
            scope=lp.temp_var_scope.PRIVATE) for name in ctx.cse_names]
    data = global_temps + cse_temps + ["..."]

    knl = lp.make_kernel(
        [domain],
        instructions + ctx.subst_rules,
        data,
        name=kernel_name)

    # FIXME: Dealing with the Island problem
    # Commenting(and not deleting) it as it maybe used for temporary
    # compilation. A long term solution is in process
    '''
    for insn_id, cse_assignment in enumerate(ctx.assignments):
        var_name, free_indices, _ = cse_assignment
        for iname in free_indices:
            knl = lp.duplicate_inames(knl, iname, "writes:"+var_name)
    '''

    return knl

# }}}

# vim: foldmethod=marker
