import numpy.typing as npt
import numpy as np
from typing import Dict, Union, List, Optional
import pycalphad.variables as v
from pycalphad.core.composition_set import CompositionSet
from pycalphad.core.solver import Solver
from pycalphad.property_framework.types import ComputableProperty, DotDerivativeDeltas, \
    DifferentiableComputableProperty, ConditionableComputableProperty
from pycalphad.property_framework import units
from copy import copy

class ModelComputedProperty(object):
    def __init__(self, model_attr_name: str, phase_name: Optional[str] = None):
        self.implementation_units = getattr(units, model_attr_name + '_implementation_units', '')
        self.display_units = getattr(units, model_attr_name + '_display_units', '')
        self.display_name = getattr(units, model_attr_name + '_display_name', model_attr_name)
        self.model_attr_name = model_attr_name
        self.phase_name = phase_name

    def __getitem__(self, new_units: str) -> "ModelComputedProperty":
        "Get ModelComputedProperty with different display units"
        newobj = copy(self)
        newobj.display_units = new_units
        return newobj

    def expand_wildcard(self, phase_names):
        return [self.__class__(self.model_attr_name, phase_name) for phase_name in phase_names]

    def __str__(self):
        result = self.model_attr_name
        if self.phase_name is not None:
            result += f'({self.phase_name})'
        return result

    def __eq__(self, other):
        if self is other:
            return True
        if self.__class__ != other.__class__:
            return False
        if self.__dict__ == other.__dict__:
            return True
        return False

    def __hash__(self):
        return hash(str(self))

    @property
    def shape(self):
        return tuple()

    @property
    def multiplicity(self):
        if self.phase_name is not None:
            tokens = self.phase_name.split('#')
            if len(tokens) > 1:
                return int(tokens[1])
            else:
                return 1
        else:
            return None

    def compute_property(self, compsets: List[CompositionSet], cur_conds: Dict[str, float], chemical_potentials: npt.ArrayLike) -> npt.ArrayLike:
        if self.phase_name is None:
            return np.nansum([compset.NP*self._compute_per_phase_property(compset, cur_conds) for compset in compsets])
        else:
            tokens = self.phase_name.split('#')
            phase_name = tokens[0]
            if len(tokens) > 1:
                multiplicity = int(tokens[1])
            else:
                multiplicity = 1
            multiplicity_seen = 0
            for compset in compsets:
                if compset.phase_record.phase_name != phase_name:
                    continue
                multiplicity_seen += 1
                if multiplicity == multiplicity_seen:
                    return self._compute_per_phase_property(compset, cur_conds)
            return np.nan


    def dot_derivative(self, compsets, cur_conds, chemical_potentials, deltas: DotDerivativeDeltas) -> npt.ArrayLike:
        "Compute dot derivative with self as numerator, with the given deltas"
        state_variables = compsets[0].phase_record.state_variables
        grad_values = self._compute_property_gradient(compsets, cur_conds, chemical_potentials)

        # Sundman et al, 2015, Eq. 73
        dot_derivative = np.nan
        for idx, compset in enumerate(compsets):
            if compset.NP == 0 and not (compset.fixed):
                continue
            func_value = self._compute_per_phase_property(compset, cur_conds)
            if np.isnan(func_value):
                continue
            if np.isnan(dot_derivative):
                dot_derivative = 0.0
            grad_value = grad_values[idx]
            delta_sitefracs = deltas.delta_sitefracs[idx]

            if self.phase_name is None:
                dot_derivative += deltas.delta_phase_amounts[idx] * func_value
                dot_derivative += compset.NP * np.dot(deltas.delta_statevars, grad_value[:len(state_variables)])
                dot_derivative += compset.NP * np.dot(delta_sitefracs, grad_value[len(state_variables):])
            else:
                dot_derivative += np.dot(deltas.delta_statevars, grad_value[:len(state_variables)])
                dot_derivative += np.dot(delta_sitefracs, grad_value[len(state_variables):])
        return dot_derivative

    def _compute_per_phase_property(self, compset: CompositionSet, cur_conds: Dict[str, float]) -> float:
        out = np.atleast_1d(np.zeros(1))
        compset.phase_record.prop(out, compset.dof, self.model_attr_name.encode('utf-8'))
        return out[0]

    def _compute_property_gradient(self, compsets, cur_conds, chemical_potentials):
        "Compute partial derivatives of property with respect to degrees of freedom of given CompositionSets"
        if self.phase_name is not None:
            tokens = self.phase_name.split('#')
            phase_name = tokens[0]
        result = [np.zeros(compset.dof.shape[0]) for compset in compsets]
        multiplicity_seen = 0
        for cs_idx, compset in enumerate(compsets):
            if (self.phase_name is not None) and (compset.phase_record.phase_name != phase_name):
                continue
            if self.multiplicity is not None:
                multiplicity_seen += 1
                if self.multiplicity != multiplicity_seen:
                    continue
            grad = np.zeros((compset.dof.shape[0]))
            compset.phase_record.prop_grad(grad, compset.dof, self.model_attr_name.encode('utf-8'))
            result[cs_idx][:] = grad
        return result

def as_property(inp: Union[str, ComputableProperty]) -> ComputableProperty:
    if isinstance(inp, ComputableProperty):
        return inp
    elif not isinstance(inp, str):
        raise TypeError(f'{inp} is not a ComputableProperty')
    dot_tokens = inp.split('.')
    if len(dot_tokens) == 2:
        numerator, denominator = dot_tokens
        numerator = as_property(numerator)
        denominator = as_property(denominator)
        return DotDerivativeComputedProperty(numerator, denominator)
    try:
        begin_parens = inp.index('(')
        end_parens = inp.index(')')
    except ValueError:
        begin_parens = len(inp)
        end_parens = len(inp)

    specified_prop = inp[:begin_parens].strip()

    prop = getattr(v, specified_prop, None)
    if prop is None:
        prop = ModelComputedProperty
    if begin_parens != end_parens:
        specified_args = tuple(x.strip() for x in inp[begin_parens+1:end_parens].split(','))
        if not isinstance(prop, type):
            prop_instance = type(prop)(*((specified_prop,)+specified_args))
        else:
            if issubclass(prop, v.StateVariable):
                prop_instance = prop(*(specified_args))
            else:
                prop_instance = prop(*((specified_prop,)+specified_args))
    else:
        if isinstance(prop, type):
            prop = prop(specified_prop)
        prop_instance = prop
    return prop_instance

class DotDerivativeComputedProperty:
    def __init__(self, numerator: DifferentiableComputableProperty, denominator: ConditionableComputableProperty):
        self.numerator = as_property(numerator)
        if not isinstance(self.numerator, DifferentiableComputableProperty):
            raise TypeError(f'{self.numerator} is not a differentiable property')
        self.denominator = as_property(denominator)
        if not isinstance(self.denominator, ConditionableComputableProperty):
            raise TypeError(f'{self.denominator} cannot be used in the denominator of a dot derivative')

    @property
    def shape(self):
        return tuple()

    @property
    def implementation_units(self):
        return str(units.ureg.Unit(self.numerator.implementation_units) / units.ureg.Unit(self.denominator.implementation_units))

    _display_units = None
    @property
    def display_units(self):
        if self._display_units is not None:
            return self._display_units
        else:
            return str(units.ureg.Unit(self.numerator.display_units) / units.ureg.Unit(self.denominator.display_units))

    @display_units.setter
    def display_units(self, val):
        self._display_units = val

    def __getitem__(self, new_units: str) -> "DotDerivativeComputedProperty":
        "Get DotDerivativeComputedProperty with different display units"
        newobj = copy(self)
        newobj.display_units = new_units
        return newobj

    @property
    def display_name(self):
        return str(self)

    def compute_property(self, compsets, cur_conds, chemical_potentials):
        solver = Solver()
        spec = solver.get_system_spec(compsets, cur_conds)
        state = spec.get_new_state(compsets)
        state.chemical_potentials[:] = chemical_potentials
        state.recompute(spec)
        deltas = self.denominator.dot_deltas(spec, state)
        return self.numerator.dot_derivative(compsets, cur_conds, chemical_potentials, deltas)

    def __str__(self):
        return str(self.numerator)+'.'+str(self.denominator)

    def __eq__(self, other):
        if self is other:
            return True
        if self.__class__ != other.__class__:
            return False
        if self.__dict__ == other.__dict__:
            return True
        return False

    def __hash__(self):
        return hash(str(self))