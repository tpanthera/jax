# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import itertools as it

from . import partial_eval as pe
from .. import core as core
from ..core import Trace, Tracer, new_master, get_aval, call_p, Primitive, Literal
from ..ad_util import (add_jaxvals, add_jaxvals_p, zeros_like_jaxval, zeros_like_aval,
                       zeros_like_p, zero, Zero)
from ..abstract_arrays import raise_to_shaped
from ..util import unzip2, unzip3, safe_map, safe_zip, partial
from ..tree_util import process_pytree, build_tree, register_pytree_node, tree_map
from ..linear_util import thunk, staged, transformation, transformation_with_aux, wrap_init
from ..api_util import flatten_fun, flatten_fun_nokwargs  # TODO: can we avoid this?
from ..tree_util import tree_flatten, tree_unflatten

from six.moves import builtins, reduce

zip = safe_zip
map = safe_map
def identity(x): return x


def jvp(fun, has_aux=False, instantiate=True):
  if not has_aux:
    return jvpfun(jvp_subtrace(fun), instantiate)
  else:
    fun, aux = jvp_subtrace_aux(fun, instantiate)
    return jvpfun(fun, instantiate), aux

@transformation
def jvpfun(instantiate, primals, tangents):
  with new_master(JVPTrace) as master:
    out_primal, out_tangent = yield (master, primals, tangents), {}
    del master
  out_tangent = instantiate_zeros_at(instantiate, out_primal, out_tangent)
  yield (out_primal, out_tangent)


@transformation
def jvp_subtrace(master, primals, tangents):
  trace = JVPTrace(master, core.cur_sublevel())
  for x in list(primals) + list(tangents):
    if isinstance(x, Tracer):
      assert x.trace.level < trace.level
  ans = yield map(partial(JVPTracer, trace), primals, tangents), {}
  out_tracers = map(trace.full_raise, ans)
  yield unzip2([(out_tracer.primal, out_tracer.tangent)
                for out_tracer in out_tracers])

@transformation_with_aux
def jvp_subtrace_aux(instantiate, master, primals, tangents):
  trace = JVPTrace(master, core.cur_sublevel())
  for x in list(primals) + list(tangents):
    if isinstance(x, Tracer):
      assert x.trace.level < trace.level
  ans, aux = yield map(partial(JVPTracer, trace), primals, tangents), {}
  out_tracer, aux_tracer = map(trace.full_raise, (ans, aux))
  out_primal, out_tangent = out_tracer.primal, out_tracer.tangent
  aux = aux_tracer.primal  # ignore aux tangent
  out_tangent = instantiate_zeros_at(instantiate, out_primal, out_tangent)
  yield (out_primal, out_tangent), aux

def linearize(traceable, *primals, **kwargs):
  has_aux = kwargs.pop('has_aux', False)
  if not has_aux:
    jvpfun = jvp(traceable)
  else:
    assert False, "TODO"
    jvpfun, aux = jvp(traceable, has_aux=True)

  in_pvals = (tuple(pe.PartialVal((None, p)) for p in primals)
            + tuple(pe.PartialVal((get_aval(p).at_least_vspace(), core.unit))
                    for p in primals))
  _, in_tree = tree_flatten(((primals, primals), {}))
  jvpfun_flat, out_tree = flatten_fun(jvpfun, in_tree)
  jaxpr, out_pvals, consts = pe.trace_to_jaxpr(jvpfun_flat, in_pvals)
  pval_primals, pval_tangents = tree_unflatten(out_tree(), out_pvals)
  aval_primals, const_primals = unzip2(pval_primals)
  assert all(aval_primal is None for aval_primal in aval_primals)
  if not has_aux:
    return const_primals, pval_tangents, jaxpr, consts
  else:
    return const_primals, pval_tangents, jaxpr, consts, aux()

def vjp(traceable, primals, has_aux=False):
  if not has_aux:
    out_primals, pvals, jaxpr, consts = linearize(traceable, *primals)
  else:
    out_primals, pvals, jaxpr, consts, aux = linearize(traceable, *primals, has_aux=True)
  def vjp_(*cts):
    cts = tuple(map(ignore_consts, cts, pvals))
    dummy_primals_and_cts = (core.unit,) * len(cts) + cts
    dummy_args = (None,) * len(jaxpr.invars)
    _, arg_cts = backward_pass(jaxpr, consts, (), dummy_args, dummy_primals_and_cts)
    arg_cts = arg_cts[len(primals):]
    return map(instantiate_zeros, primals, arg_cts)

  if not has_aux:
    return out_primals, vjp_
  else:
    return out_primals, vjp_, aux

def ignore_consts(ct, pval):
  aval, const = pval
  if isinstance(aval, core.AbstractValue):
    return ct
  elif aval is None:
    return core.unit
  else:
    raise TypeError(aval)

def unpair_pval(pval):
  aval, const = pval
  const_1, const_2 = const
  if aval is None:
    return (None, const_1), (None, const_2)
  else:
    aval_1, aval_2 = aval
    return (aval_1, const_1), (aval_2, const_2)

def backward_pass(jaxpr, consts, freevar_vals, args, cotangents_in):
  def write_cotangent(v, ct):
    # assert v not in primal_env
    if ct is not None:
      ct_env[v] = add_tangents(ct_env[v], ct) if v in ct_env else ct

  def read_cotangent(v):
    return ct_env.get(v, zero)

  def read_primal(v):
    if type(v) is Literal:
      return v.val
    else:
      return primal_env.get(v)

  def write_primal(v, val):
    if val is not None:
      primal_env[v] = val

  primal_env = {}
  map(write_primal, jaxpr.constvars, consts)
  map(write_primal, jaxpr.freevars, freevar_vals)
  map(write_primal, jaxpr.invars, args)

  ct_env = {}
  map(write_cotangent, jaxpr.outvars, cotangents_in)
  for eqn in jaxpr.eqns[::-1]:
    invals = map(read_primal, eqn.invars)
    if eqn.primitive.multiple_results:
      cts_in = map(read_cotangent, eqn.outvars)
    else:
      cts_in, = map(read_cotangent, eqn.outvars)
    if eqn.bound_subjaxprs:
      (subjaxpr, const_vars, bound_vars), = eqn.bound_subjaxprs
      sub_consts = map(read_primal, const_vars)
      sub_freevar_vals = map(read_primal, bound_vars)
      ct_free_vars_out, cts_out = get_primitive_transpose(eqn.primitive)(
          eqn.params, subjaxpr, sub_consts, sub_freevar_vals, invals, cts_in)
      map(write_cotangent, bound_vars, ct_free_vars_out)
    else:
      cts_out = get_primitive_transpose(eqn.primitive)(cts_in, *invals, **eqn.params)
    map(write_cotangent, eqn.invars, cts_out)

  freevar_cts = map(read_cotangent, jaxpr.freevars)
  cotangents_out = map(read_cotangent, jaxpr.invars)
  return freevar_cts, cotangents_out

def get_primitive_transpose(p):
  try:
    return primitive_transposes[p]
  except KeyError:
    raise NotImplementedError(
      "Reverse-mode differentiation rule for '{}' not implemented".format(p))

class JVPTrace(Trace):

  def pure(self, val):
    return JVPTracer(self, val, zero)

  def lift(self, val):
    return JVPTracer(self, val, zero)

  def sublift(self, val):
    return JVPTracer(self, val.primal, val.tangent)

  def process_primitive(self, primitive, tracers, params):
    primals_in = [t.primal for t in tracers]
    tangents_in = [t.tangent for t in tracers]
    try:
      jvp = primitive_jvps[primitive]
    except KeyError:
      raise NotImplementedError(
          "Forward-mode differentiation rule for '{}' not implemented"
          .format(primitive))
    primal_out, tangent_out = jvp(primals_in, tangents_in, **params)
    return JVPTracer(self, primal_out, tangent_out)

  def process_call(self, call_primitive, f, tracers, params):
    primals = [t.primal for t in tracers]
    tangents = [t.tangent for t in tracers]
    nonzero_tangents, in_tree_def = tree_flatten(tangents)
    f_jvp, out_tree_def = traceable(jvp_subtrace(f, self.master), len(primals), in_tree_def)
    result = call_primitive.bind(f_jvp, *(primals + nonzero_tangents), **params)
    primal_out, tangent_out = tree_unflatten(out_tree_def(), result)
    return [JVPTracer(self, p, t) for p, t in zip(primal_out, tangent_out)]

  def post_process_call(self, call_primitive, out_tracer, params):
    assert False  # TODO: update to no-tuples
    out_jtuple, tree_def = tree_to_jaxtuples((out_tracer.primal, out_tracer.tangent))
    master = self.master
    def todo(x):
      trace = JVPTrace(master, core.cur_sublevel())
      return JVPTracer(trace, *build_tree(tree_def, x))

    return out_jtuple, todo

  def join(self, xt, yt):
    xz, yz = xt is zero, yt is zero
    if xz == yz:
      return xt, yt
    elif yz and not xz:
      return xt, zeros_like_jaxval(xt)
    elif xz and not yz:
      return zeros_like_jaxval(yt), yt
    else:
      raise TypeError((xt, yt))


class JVPTracer(Tracer):
  __slots__ = ['primal', 'tangent']

  def __init__(self, trace, primal, tangent):
    if not core.skip_checks:
      _primal_tangent_shapes_match(primal, tangent)
    self.trace = trace
    self.primal = primal
    self.tangent = tangent

  @property
  def aval(self):
    # TODO(dougalm): add epsilon ball
    return get_aval(self.primal)

  def full_lower(self):
    if self.tangent is zero:
      return core.full_lower(self.primal)
    else:
      return self

def _primal_tangent_shapes_match(primal, tangent):
  if tangent is not zero:
    primal_aval = raise_to_shaped(get_aval(primal))
    tangent_aval = raise_to_shaped(get_aval(tangent))
    assert primal_aval == tangent_aval

# -------------------- Primitives --------------------


primitive_jvps = {}
composite_jvps = {}

primitive_transposes = {}


def deflinear(primitive, transpose_rule):
  primitive_jvps[primitive] = partial(linear_jvp, primitive)
  primitive_transposes[primitive] = partial(linear_transpose, transpose_rule)

def linear_jvp(primitive, primals, tangents, **params):
  val_out = primitive.bind(*primals, **params)
  if all(tangent is zero for tangent in tangents):
    return val_out, zero
  else:
    tangents = map(instantiate_zeros, primals, tangents)
    return val_out, primitive.bind(*tangents, **params)

def linear_transpose(transpose_rule, cotangent, *args, **kwargs):
  return zero if cotangent is zero else transpose_rule(cotangent, **kwargs)


def defjvp(primitive, *jvprules):
  assert isinstance(primitive, Primitive)
  primitive_jvps[primitive] = partial(standard_jvp, jvprules, primitive)


def standard_jvp(jvprules, primitive, primals, tangents, **params):
  val_out = primitive.bind(*primals, **params)
  tangents_out = [rule(t, *primals, **params) for rule, t in zip(jvprules, tangents)
                  if rule is not None and t is not zero]
  return val_out, reduce(add_tangents, tangents_out, zero)

def defjvp2(primitive, *jvprules):
  assert isinstance(primitive, Primitive)
  primitive_jvps[primitive] = partial(standard_jvp2, jvprules, primitive)

def standard_jvp2(jvprules, primitive, primals, tangents, **params):
  val_out = primitive.bind(*primals, **params)
  tangents_out = (rule(t, val_out, *primals, **params) for rule, t in zip(jvprules, tangents)
                  if rule is not None and t is not zero)
  return val_out, reduce(add_tangents, tangents_out, zero)

def add_tangents(x, y):
  if x is zero:
    return y
  elif y is zero:
    return x
  else:
    return add_jaxvals(x, y)


def defvjp_argnums(prim, custom_vjp):
  name = prim.name

  def fun_jvp(xs, ts, **params):
    params['vjp_argnums'] = tuple(i for i, t in enumerate(ts) if t is not zero)
    ts = map(instantiate_zeros, xs, ts)  # TODO(mattjj): avoid instantiation?
    primal_out, tangent_out = fun_jvp_p.bind(pack(xs), pack(ts), **params)
    return primal_out, tangent_out
  primitive_jvps[prim] = fun_jvp

  fun_jvp_p = core.Primitive('{name}_jvp'.format(name=name))
  def fun_jvp_partial_eval(trace, *tracers, **params):
    primals_tracer, tangents_tracer = tracers
    argnums = params.pop('vjp_argnums')
    primal_out, vjp_py = custom_vjp(argnums, *primals_tracer, **params)

    in_aval = raise_to_shaped(get_aval(primal_out))
    ct_pval = pe.PartialVal((in_aval, core.unit))
    vjp_jaxpr, out_pval, residuals = pe.trace_unwrapped_to_jaxpr(
        lambda ct: pack(vjp_py(ct)), (ct_pval,), instantiate=False)
    out_pv, out_const = out_pval
    tangent_out = fun_lin_p.bind(out_const, pack(residuals), tangents_tracer,
                                 in_aval=in_aval, out_pv=out_pv, vjp_jaxpr=vjp_jaxpr)

    return pack((primal_out, tangent_out))
  pe.custom_partial_eval_rules[fun_jvp_p] = fun_jvp_partial_eval

  fun_lin_p = core.Primitive('{name}_lin'.format(name=name))
  fun_lin_p.def_abstract_eval(lambda c, r, ts, in_aval, out_pv, vjp_jaxpr: in_aval)
  def fun_lin_transpose(ct, out_const, residuals, ts, in_aval, out_pv, vjp_jaxpr):
    assert ts is None and out_const is not None and residuals is not None
    ans = core.eval_jaxpr(vjp_jaxpr, residuals, (), ct)
    out = pe.merge_pvals(ans, pe.PartialVal((out_pv, out_const)))
    return [None, None, out]
  primitive_transposes[fun_lin_p] = fun_lin_transpose

def defvjp_all(prim, custom_vjp):
  # see https://github.com/google/jax/pull/636
  def custom_vjp_(argnums, *args, **params):
    return custom_vjp(*args, **params)
  defvjp_argnums(prim, custom_vjp_)

def defvjp(prim, *vjps):
  def vjpmaker(*primals):
    ans = prim.bind(*primals)
    vjpfun = lambda ct: [vjp(ct, *primals) if vjp else zeros_like_jaxval(x)
                         for x, vjp in zip(primals, vjps)]
    return ans, vjpfun
  defvjp_all(prim, vjpmaker)

def defvjp2(prim, *vjps):
  def vjpmaker(*primals):
    ans = prim.bind(*primals)
    vjpfun = lambda ct: [vjp(ct, ans, *primals) if vjp else zeros_like_jaxval(x)
                         for x, vjp in zip(primals, vjps)]
    return ans, vjpfun
  defvjp_all(prim, vjpmaker)


def defbilinear_broadcasting(bcast, prim, lhs_rule, rhs_rule):
  assert isinstance(prim, Primitive)
  lhs_jvp = lambda g, x, y, **kwargs: prim.bind(bcast(g, y), y, **kwargs)
  rhs_jvp = lambda g, x, y, **kwargs: prim.bind(x, bcast(g, x), **kwargs)
  defjvp(prim, lhs_jvp, rhs_jvp)
  primitive_transposes[prim] = partial(bilinear_transpose, lhs_rule, rhs_rule)
defbilinear = partial(defbilinear_broadcasting, lambda g, x: g)

def bilinear_transpose(lhs_rule, rhs_rule, cotangent, x, y, **kwargs):
  assert (x is None) ^ (y is None)
  if x is None:
    out = zero if cotangent is zero else lhs_rule(cotangent, y, **kwargs)
    return out, None
  else:
    out = zero if cotangent is zero else rhs_rule(cotangent, x, **kwargs)
    return None, out


def defjvp_zero(primitive):
  assert isinstance(primitive, Primitive)
  primitive_jvps[primitive] = partial(zero_jvp, primitive)

def zero_jvp(primitive, primals, tangents, **params):
  return primitive.bind(*primals, **params), zero


deflinear(zeros_like_p, lambda t: [zero])
deflinear(core.identity_p, lambda t: (t,))
deflinear(add_jaxvals_p, lambda t: (t, t))


def instantiate_zeros_at(instantiate, example, tangent):
  assert type(instantiate) is bool
  return instantiate_zeros(example, tangent) if instantiate else tangent

def instantiate_zeros(example, tangent):
  if tangent is zero:
    return zeros_like_jaxval(example)
  else:
    return tangent

def instantiate_zeros_aval(aval, tangent):
  if tangent is zero:
    return zeros_like_aval(aval)
  else:
    return tangent

@transformation_with_aux
def traceable(num_primals, in_tree_def, *new_primals_and_tangents):
  new_primals  = new_primals_and_tangents[:num_primals]
  new_tangents = new_primals_and_tangents[num_primals:]
  new_tangents = tree_unflatten(in_tree_def, new_tangents)
  primal_out, tangent_out = yield (new_primals, new_tangents), {}
  out_flat, tree_def = tree_flatten((primal_out, tangent_out))
  yield out_flat, tree_def

def call_transpose(primitive, params, jaxpr, consts, freevar_vals, args, ct):
  all_args, in_tree_def = tree_flatten((consts, freevar_vals, args, ct))
  fun = wrap_init(partial(backward_pass, jaxpr))
  fun, out_tree = flatten_fun_nokwargs(fun, in_tree_def)
  out_flat = primitive.bind(fun, *all_args, **params)
  return tree_unflatten(out_tree(), out_flat)

@transformation_with_aux
def transposed_mapped(jaxpr, in_tree_def, freevar_vals, args):
  args, consts, ct = args
  args, ct = build_tree(in_tree_def, (args, ct))
  freevar_cts, cotangents_out = yield (jaxpr, consts, freevar_vals, args, ct), {}
  out_jtuple, tree_def = tree_to_jaxtuples((cotangents_out, freevar_cts))
  yield out_jtuple, tree_def

def map_transpose(primitive, params, jaxpr, consts, freevar_vals, args, ct):
  jaxpr, = jaxpr
  consts, = consts
  freevar_vals, = freevar_vals
  (args, ct), in_tree_def = tree_to_jaxtuples((args, ct))
  fun = wrap_init(backward_pass)
  fun, out_tree_def = transposed_mapped(fun, jaxpr, in_tree_def, tuple(freevar_vals))
  all_args = pack((pack(args), pack(consts), ct))
  ans = primitive.bind(fun, all_args, **params)
  cts_out, freevar_cts = build_tree(out_tree_def(), ans)
  freevar_cts = tree_map(lambda x: x.sum(0), freevar_cts)
  return cts_out, freevar_cts

def put_zeros(pack, isnonzero, x):
  if isnonzero is True:
    return x
  elif isnonzero is False:
    return zero
  else:
    return pack(map(partial(put_zeros, pack), isnonzero, x))

def strip_zeros(unit, pack, isnonzero, x):
  if isnonzero is True:
    return x
  elif isnonzero is False:
    return unit
  else:
    return pack(map(partial(strip_zeros, unit, pack), isnonzero, x))

@transformation_with_aux
def f_jvp_traceable(nonzero_components, *primal_tangent_pairs):
  assert False, "update it"
  primals, tangents = unzip2(primal_tangent_pairs)
  tangents_zeros = map(partial(put_zeros, TangentTuple), nonzero_components, tangents)
  primal_out, tangent_out = yield (primals, tangents_zeros), {}
  # TODO check output is tuple
  nonzeros_out = get_nonzeros(tangent_out)
  tangent_out_nonzero = strip_zeros(core.unit, pack, nonzeros_out, tangent_out)
  primal_tangent_pairs_out = [pack((p, t)) for p, t in zip(primal_out, tangent_out_nonzero)]
  yield pack(primal_tangent_pairs_out), nonzeros_out

def jvp_jaxpr(jaxpr, nonzeros, instantiate):
  # jaxpr :: d -> a -> b -> (c1, c2)
  # avals = (d, a, b)
  # f :: d -> a -> b -> (c1, c2)
  f = wrap_init(core.jaxpr_as_fun(jaxpr))
  f_jvp, out_nonzeros = f_jvp_traceable(jvp(f, instantiate=instantiate), nonzeros)
  # f_jvp :: (d, d') -> (a, a') -> (b, b') -> ((c1, c1'), (c2, c2'))
  tangent_avals = map(partial(strip_zeros, core.AbstractTuple(()), core.AbstractTuple),
                      nonzeros, jaxpr.in_avals)
  pt_pvals = [pe.PartialVal((core.AbstractTuple((p_aval, t_aval)), core.unit))
              for p_aval, t_aval in zip(jaxpr.in_avals, tangent_avals)]
  jaxpr_out, pval_out, literals_out = pe.trace_to_jaxpr(
      f_jvp, pt_pvals, instantiate=True)
  # jaxpr_out :: (d, d') -> (a, a') -> (b, b') -> ((c1, c1'), (c2, c2'))
  # out_nonzeros :: (nonzeros(c1), nonzeros(c2))
  in_avals = tuple(map(core.AbstractTuple, zip(jaxpr.in_avals, tangent_avals)))
  out_aval, _ = pval_out
  jaxpr_out = core.TypedJaxpr(jaxpr_out, literals_out, in_avals, out_aval)
  return jaxpr_out, out_nonzeros()


primitive_transposes[core.call_p] = partial(call_transpose, call_p)
