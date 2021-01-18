"""
Support for reading ChemSage DAT files.

This implementation is based on a careful reading of Thermochimica source code (with some trial-and-error).
Thermochimica is software written by M. H. A. Piro and released under the BSD License.

Careful of a gotcha:  `obj.setParseAction` modifies the object in place but calling it a name makes a new object:

`obj = Word(nums); obj is obj.setParseAction(lambda t: t)` is True
`obj = Word(nums); obj is obj('abc').setParseAction(lambda t: t)` is False



"""

import numpy as np
from dataclasses import dataclass
from pycalphad import Database
from collections import deque
from sympy import S, log, Piecewise, And
from pycalphad import variables as v


# From ChemApp Documentation, section 11.1 "The format of a ChemApp data-file"
# We use a leading zero term because the data file's indices are 1-indexed and
# this prevents us from needing to shift the indicies.
GIBBS_TERMS = (S.Zero, S.One, v.T, v.T*log(v.T), v.T**2, v.T**3, 1/v.T)
# TODO: HEAT_CAPACITY_TERMS:
# We need to transform these into Gibbs energies, i.e.
# G = H - TS = (A + integral(CpdT)) + T(B + integral(Cp/T dT))
# Terms are
# Cp = (S.Zero, S.One, v.T, v.T**2, v.T**(-2))
EXCESS_TERMS = (S.Zero, S.One, v.T, v.T*log(v.T), v.T**2, v.T**3, 1/v.T, v.P, v.P**2)


class TokenParser(deque):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.token_index = 0

    def parse(self, cls: type):
        self.token_index += 1
        return cls(self.popleft())

    def parseN(self, N: int, cls: type):
        if N < 1:
            raise ValueError(f'N must be >=1, got {N}')
        return [self.parse(cls) for _ in range(N)]


@dataclass
class Header:
    list_soln_species_count: [int]
    num_stoich_phases: int
    pure_elements: [str]
    pure_elements_mass: [float]
    gibbs_coefficient_idxs: [int]
    excess_coefficient_idxs: [int]


@dataclass
class AdditionalCoefficientPair:
    coefficient: float
    exponent: float

    def expr(self):
        return self.coefficient * v.T**(self.exponent)


@dataclass
class PTVmTerms:
    terms: [float]


@dataclass
class IntervalBase:
    T_max: float

    def expr(self):
        raise NotImplementedError("Subclasses of IntervalBase must define an expression for the energy")

    def cond(self, T_min=298.15):
        return (T_min <= v.T) & (v.T < self.T_max)

    def expr_cond_pair(self, *args, T_min=298.15, **kwargs):
        """Return an (expr, cond) tuple used to construct Piecewise expressions"""
        expr = self.expr(*args, **kwargs)
        cond = self.cond(T_min)
        return (expr, cond)


@dataclass
class IntervalG(IntervalBase):
    coefficients: [float]
    additional_coeff_pairs: [AdditionalCoefficientPair]
    PTVm_terms: [PTVmTerms]

    def expr(self, indices):
        """Return an expression for the energy in this temperature interval"""
        energy = S.Zero
        # Add fixed energy terms
        energy += sum([C*GIBBS_TERMS[i] for C, i in zip(self.coefficients, indices)])
        # Add additional energy coefficient-exponent pair terms
        energy += sum([addit_term.expr() for addit_term in self.additional_coeff_pairs])
        # P-T molar volume terms, not supported
        if len(self.PTVm_terms) > 0:
            raise NotImplementedError("P-T molar volume terms are not supported")
        return energy


@dataclass
class IntervalCP(IntervalBase):
    # Fixed term heat capacity interval with extended terms
    H298: float
    S298: float
    CP_coefficients: float
    H_trans: float
    additional_coeff_pairs: [AdditionalCoefficientPair]
    PTVm_terms: [PTVmTerms]

    def expr(self, indices, T_min=298.15):
        raise NotImplementedError("Gibbs energy equations defined with heat capacity terms are not yet supported.")


@dataclass
class Quadruplet:
    quadruplet_idxs: [int]  # exactly four
    quadruplet_coordinations: [float]  # exactly four


@dataclass
class ExcessQuadruplet:
    mixing_type: int
    mixing_code: str
    mixing_const: [int]  # exactly four
    mixing_exponents: [int]  # exactly four
    junk: [float]  # exactly twelve
    additional_mixing_const: int
    additional_mixing_exponent: int
    excess_coeffs: [float]


@dataclass
class Endmember():
    species_name: str
    gibbs_eq_type: str
    stoichiometry_pure_elements: [float]
    intervals: [IntervalBase]

    def expr(self, indices):
        """Return a Piecewise (in temperature) energy expression for this endmember (i.e. only the data from the energy intervals)"""
        T_min = 298.15
        expr_cond_pairs = []
        for interval in self.intervals:
            expr_cond_pairs.append(interval.expr_cond_pair(indices, T_min=T_min))
            T_min = interval.T_max
        return Piecewise(*expr_cond_pairs, evaluate=False)

    def constituents(self, pure_elements: [str]) -> {str: float}:
        return {el: amnt for el, amnt in zip(pure_elements, self.stoichiometry_pure_elements) if amnt != 0.0}

    def constituent_array(self) -> [[str]]:
        return [[sp] for sp in self.species_name.split(':')]

    def species(self, pure_elements) -> [v.Species]:
        if len(self.species_name.split(':')) > 1:
            # If given in sublattice notation, assume species are pure elements
            # i.e. multi-sublattice models cannot have associates
            all_species = self.species_name.split(':')
            return np.unique([v.Species(sp_str) for sp_str in all_species]).tolist()
        else:
            # We only have one sublattice, this can be a non-pure element species
            return [v.Species(self.species_name, constituents=self.constituents(pure_elements))]

    def insert(self, dbf: Database, phase_name: str, pure_elements: [str], gibbs_coefficient_idxs: [int]):
        dbf.add_parameter('G', phase_name, self.constituent_array(),
                          0, self.expr(gibbs_coefficient_idxs), force_insert=False)


@dataclass
class EndmemberMagnetic(Endmember):
    curie_temperature: float
    magnetic_moment: float

    def insert(self, dbf: Database, phase_name: str, pure_elements: [str], gibbs_coefficient_idxs: [int]):
        # add Gibbs energy
        super().insert(dbf, phase_name, pure_elements, gibbs_coefficient_idxs)

        # also add magnetic parameters
        dbf.add_parameter('BMAG', phase_name, self.constituent_array(),
                          0, self.magnetic_moment, force_insert=False)
        dbf.add_parameter('TC', phase_name, self.constituent_array(),
                          0, self.curie_temperature, force_insert=False)


@dataclass
class EndmemberRealGas(Endmember):
    # Tsonopoulos data
    Tc: float
    Pc: float
    Vc: float
    acentric_factor: float
    dipole_moment: float

    def insert(*args, **kwargs):
        raise NotImplementedError("Inserting parameters for real gas Endmembers is not supported.")


@dataclass
class EndmemberAqueous(Endmember):
    charge: float

    def insert(*args, **kwargs):
        raise NotImplementedError("Inserting parameters for aqueous Endmembers is not supported.")


@dataclass
class EndmemberSUBQ(Endmember):
    stoichiometry_quadruplet: [float]
    zeta: float


@dataclass
class ExcessCEF:
    interacting_species_idxs: [int]
    parameter_order: int
    coefficients: [float]

    def expr(self, indices):
        """Return an expression for the energy in this temperature interval"""
        energy = S.Zero
        # Add fixed energy terms
        energy += sum([C*EXCESS_TERMS[i] for C, i in zip(self.coefficients, indices)])
        return energy

    def _map_const_idxs_to_subl_idxs(self, num_subl_species: [int]) -> [[int]]:
        """
        Converts from one-indexed linear phase species indices to zero-indexed
        sublattice species indices.

        Parameters
        ----------
        num_subl_species: [int]
            Number of species in each sublattice, i.e. [1, 2, 1] could
            correspond to a sublattice model of [['A'], ['A', 'B'], ['C']]

        Returns
        -------
        [[int]] - a list of species index lists

        Examples
        --------
        >>> assert ExcessCEF([1, 2, 3, 4], 0, [0])._map_const_idxs_to_subl_idxs([2, 3]) == [[0, 1], [0, 1]]
        >>> assert ExcessCEF([1, 3, 4], 0, [0])._map_const_idxs_to_subl_idxs([2, 3]) == [[0], [0, 1]]
        >>> assert ExcessCEF([1, 2], 0, [0])._map_const_idxs_to_subl_idxs([4]) == [[0, 1]]
        >>> assert ExcessCEF([1, 2, 3], 0, [0])._map_const_idxs_to_subl_idxs([3]) == [[0, 1, 2]]
        >>> assert ExcessCEF([1, 2, 3, 4], 0, [0])._map_const_idxs_to_subl_idxs([1, 1, 2]) == [[0], [0], [0, 1]]
        """
        cum_num_subl_species = np.cumsum(num_subl_species)
        # initialize an empty sublattice model
        subl_species_idxs = [[] for _ in range(len(num_subl_species))]
        for linear_idx in self.interacting_species_idxs:
            # Find which sublattice this species belongs to by seeing that the
            # linear_idx is contained within the cumulative number of species
            # on each sublattice
            subl_idx = (cum_num_subl_species >= linear_idx).tolist().index(True)
            # Determine the index of the species within the sublattice
            # This is still the one-indexed value
            if subl_idx == 0:
                subl_sp_idx = linear_idx
            else:
                subl_sp_idx = linear_idx - cum_num_subl_species[subl_idx - 1]
            # convert one-indexed species index to zero-indexed
            subl_sp_idx -= 1
            # add the species index to the right sublattice
            subl_species_idxs[subl_idx].append(subl_sp_idx)
        # all sublattices must be occupied
        assert all(len(subl) > 0 for subl in subl_species_idxs)
        return subl_species_idxs

    def constituent_array(self, phase_constituents: [[str]]) -> [[str]]:
        """
        Return the constituent array of this interaction using the entire phase
        sublattice model.

        This doesn't take into account any re-ordering of the phase_constituents
        or interacting_species_idxs. All mapping on to proper v.Species objects
        occurs in Database.add_parameter.

        Examples
        --------
        >>> phase_constituents = [['A'], ['A', 'B'], ['A', 'B', 'C']]
        >>> ex = ExcessCEF([1, 2, 4, 6], 0, [0])
        >>> ix_const_arr = ex.constituent_array(phase_constituents)
        >>> assert ix_const_arr == [['A'], ['A'], ['A', 'C']]
        """
        num_subl_species = [len(subl) for subl in phase_constituents]
        subl_species_idxs = self._map_const_idxs_to_subl_idxs(num_subl_species)
        return [[phase_constituents[subl_idx][idx] for idx in subl] for subl_idx, subl in enumerate(subl_species_idxs)]

    def insert(self, dbf: Database, phase_name: str, phase_constituents: [[str]], excess_coefficient_idxs: [int]):
        """
        Requires all Species in dbf.species to be defined.
        """
        # TODO: sorting of interaction. For any excess interaction of order
        # v>0 the order of species matters due to a (X_A - X_B)^v term. Usually
        # CALPHAD implementations sort the elements in alphabetic order, but
        # this sorting is not enforced in the ChemSage DAT format. The question
        # is whether ChemSage would convert these to alphabetic order (and flip)
        # the sign of the interaction appropriately, or whether the order of
        # X_A - X_B is preserved even if A comes after B alphabetically. For now,
        # we'll just leave the order as intended, though the pycalphad Model
        # implementation may flip the order on us, needs to be checked.
        const_array = self.constituent_array(phase_constituents)
        dbf.add_parameter('L', phase_name, const_array, self.parameter_order, self.expr(excess_coefficient_idxs), force_insert=False)


@dataclass
class ExcessCEFMagnetic:
    interacting_species_idxs: [int]
    parameter_order: int
    curie_temperature: float
    magnetic_moment: float

    def insert(self, *args, **kwargs):
        raise NotImplementedError("Inserting magnetic excess parameters is not supported.")


@dataclass
class ExcessQKTO:
    interacting_species_idxs: [int]
    interacing_species_type: [int]
    coefficients: [float]


@dataclass
class _Phase:
    phase_name: str
    phase_type: str
    endmembers: [Endmember]

    def insert(self, dbf: Database, pure_elements: [str], gibbs_coefficient_idxs: [int], excess_coefficient_idxs: [int]):
        """Enter this phase and its parameters into the Database.

        This method should call:

        * `dbf.add_phase`
        * `dbf.add_phase_constituents`
        * `dbf.add_parameter` (likely multiple times)

        """
        raise NotImplementedError(f"Subclass {type(self).__name__} of _Phase must implement `insert` to add the phase, constituents and parameters to the Database.")


@dataclass
class Phase_Stoichiometric(_Phase):
    def insert(self, dbf: Database, pure_elements: [str], gibbs_coefficient_idxs: [int], excess_coefficient_idxs: [int]):
        # TODO: magnetic model hints? Are these why there are four numbers for
        # magnetic endmembers instead of two for solution phases? Add a raise in
        # the parser instead of parsing these into nothing to find the examples.
        dbf.add_phase(self.phase_name, {}, [1.0])
        assert len(self.endmembers) == 1  # stoichiometric phase
        # define phase constituents as a species on a single sublattice, which is also the name of the phase
        # first we need to add this species to the Database
        sp_name = self.phase_name
        sp = v.Species(sp_name, constituents=self.endmembers[0].constituents(pure_elements))
        if sp in dbf.species:
            raise ValueError(f"Species {sp} already exists in the Database. Phase {self.phase_name} cannot be added.")
        else:
            dbf.species.add(sp)
        constituents = [[sp_name]]
        dbf.add_phase_constituents(self.phase_name, constituents)


@dataclass
class Phase_CEF(_Phase):
    subl_ratios: [float]
    excess_parameters: [ExcessCEF]
    magnetic_afm_factor: float
    magnetic_structure_factor: float

    def insert(self, dbf: Database, pure_elements: [str], gibbs_coefficient_idxs: [int], excess_coefficient_idxs: [int]):
        if self.magnetic_afm_factor is not None and self.magnetic_structure_factor is not None:
            # This is a magnetic model. I assumed IHJ model because I cannot
            # find any details for which model is used in the ChemSage docs.
            model_hints = {
                # The TDB syntax would define the AFM factor for FCC as -3
                # while ChemSage defines +0.333. This is likely because the
                # model divides by the AFM factor (-1/3). We convert the AFM
                # factor to the version used in the TDB/Model.
                'ihj_magnetic_afm_factor': -1/self.magnetic_afm_factor,
                'ihj_magnetic_structure_factor': self.magnetic_structure_factor,
            }
        else:
            model_hints = {}

        dbf.add_phase(self.phase_name, model_hints=model_hints, sublattices=self.subl_ratios)

        # Before we add parameters, we need to first add all the species to dbf,
        # since dbf.add_parameter takes a constituent array of string species
        # names which are mapped to Species objects
        for endmember in self.endmembers:
            for sp in endmember.species(pure_elements):
                invalid_shared_names = [(sp.name == esp.name and sp != esp) for esp in dbf.species]
                if any(invalid_shared_names):
                    # names match some already  but constituents do not
                    raise ValueError(f"A Species named {sp.name} (defined for phase {self.phase_name}) already exists in the database's species ({dbf.species}), but the constituents do not match.")
                dbf.species.add(sp)

        # Construct constituents for this phase, this loop could be merged with
        # the parameter additions above (it's not dependent like the species
        # step), but we are keeping it logically separate to make it clear how
        # it's working. This assumes that all constituents are present in
        # endmembers (i.e. there are no endmembers that are implicit).
        constituents = [[] for _ in range(len(self.subl_ratios))]
        for endmember in self.endmembers:
            for subl, const_subl in zip(endmember.constituent_array(), constituents):
                const_subl.extend(subl)
        # TODO:
        # constituent array now has all the constituents in every sublattice,
        # e.g. it could be [['A', 'A'], ['D', 'B']]
        # the question is whether the parameters are in typical Calphad
        # alphabetically sorted or if they are in FACTSAGE pure element order
        # for now, we assume alphabetical sorted order. This can easily be
        # tested by having a single phase L1 model in FACTSAGE/Thermochimica.
        sorted_constituents = [sorted(np.unique(ca)) for ca in constituents]
        dbf.add_phase_constituents(self.phase_name, sorted_constituents)

        # Now that all the species are in the database, we are free to add the parameters
        # First for endmembers
        for endmember in self.endmembers:
            endmember.insert(dbf, self.phase_name, pure_elements, gibbs_coefficient_idxs)

        # Now for excess parameters
        # TODO: We add them last since they depend on the phase's constituent
        # array. As discussed in ExcessCEF.insert, we use the built constituent
        # order above, but some models (e.g. SUBL) define the phase models
        # internally and this is thrown away by the parser currently.
        for excess_param in self.excess_parameters:
            excess_param.insert(dbf, self.phase_name, constituents, excess_coefficient_idxs)


@dataclass
class Phase_SUBQ(_Phase):
    num_pairs: int
    num_quadruplets: int
    num_subl_1_const: int
    num_subl_2_const: int
    subl_1_const: [str]
    subl_2_const: [str]
    subl_1_charges: [float]
    subl_1_chemical_groups: [int]
    subl_2_charges: [float]
    subl_2_chemical_groups: [int]
    subl_const_idx_pairs: [(int,)]
    quadruplets: [Quadruplet]
    excess_parameters: [ExcessQuadruplet]


# TODO: not yet supported
@dataclass
class Phase_RealGas(_Phase):
    endmembers: [EndmemberRealGas]


# TODO: not yet supported
@dataclass
class Phase_Aqueous(_Phase):
    endmembers: [EndmemberAqueous]


def tokenize(instring, startline=0, force_upper=False):
    if force_upper:
        return TokenParser('\n'.join(instring.upper().splitlines()[startline:]).split())
    else:
        return TokenParser('\n'.join(instring.splitlines()[startline:]).split())


def parse_header(toks: TokenParser) -> Header:
    num_pure_elements = toks.parse(int)
    num_soln_phases = toks.parse(int)
    list_soln_species_count = toks.parseN(num_soln_phases, int)
    num_stoich_phases = toks.parse(int)
    pure_elements = toks.parseN(num_pure_elements, str)
    pure_elements_mass = toks.parseN(num_pure_elements, float)
    num_gibbs_coeffs = toks.parse(int)
    gibbs_coefficient_idxs = toks.parseN(num_gibbs_coeffs, int)
    num_excess_coeffs = toks.parse(int)
    excess_coefficient_idxs = toks.parseN(num_excess_coeffs, int)
    header = Header(list_soln_species_count, num_stoich_phases, pure_elements, pure_elements_mass, gibbs_coefficient_idxs, excess_coefficient_idxs)
    return header


def parse_additional_terms(toks: TokenParser) -> [AdditionalCoefficientPair]:
    num_additional_terms = toks.parse(int)
    return [AdditionalCoefficientPair(*toks.parseN(2, float)) for _ in range(num_additional_terms)]


def parse_PTVm_terms(toks: TokenParser) -> PTVmTerms:
    # TODO: is this correct? Is there a better mapping?
    # parse molar volume terms, there seem to always be 11 terms (at least in the one file I have)
    return PTVmTerms(toks.parseN(11, float))


def parse_interval_Gibbs(toks: TokenParser, num_gibbs_coeffs, has_additional_terms, has_PTVm_terms) -> IntervalG:
    temperature_max = toks.parse(float)
    coefficients = toks.parseN(num_gibbs_coeffs, float)
    additional_coeff_pairs = parse_additional_terms(toks) if has_additional_terms else []
    # TODO: parsing for constant molar volumes
    PTVm_terms = parse_PTVm_terms(toks) if has_PTVm_terms else []
    return IntervalG(temperature_max, coefficients, additional_coeff_pairs, PTVm_terms)


def parse_interval_heat_capacity(toks: TokenParser, num_gibbs_coeffs, H298, S298, has_H_trans, has_additional_terms, has_PTVm_terms) -> IntervalCP:
    # 6 coefficients are required
    assert num_gibbs_coeffs == 6
    if has_H_trans:
        H_trans = toks.parse(float)
    else:
        H_trans = 0.0  # 0.0 will be added to the first (or only) interval
    temperature_max = toks.parse(float)
    CP_coefficients = toks.parseN(4, float)
    additional_coeff_pairs = parse_additional_terms(toks) if has_additional_terms else []
    # TODO: parsing for constant molar volumes
    PTVm_terms = parse_PTVm_terms(toks) if has_PTVm_terms else []
    return IntervalCP(temperature_max, H298, S298, CP_coefficients, H_trans, additional_coeff_pairs, PTVm_terms)


def parse_endmember(toks: TokenParser, num_pure_elements, num_gibbs_coeffs, is_stoichiometric=False):
    species_name = toks.parse(str)
    if toks[0] == '#':
        # special case for stoichiometric phases, this is a dummy species, skip it
        _ = toks.parse(str)
    gibbs_eq_type = toks.parse(int)

    # Determine how to parse the type of thermodynamic option
    has_magnetic = gibbs_eq_type > 12
    gibbs_eq_type_reduced = (gibbs_eq_type - 12) if has_magnetic else gibbs_eq_type
    is_gibbs_energy_interval = gibbs_eq_type_reduced in (1, 2, 3, 4, 5, 6)
    is_heat_capacity_interval = gibbs_eq_type_reduced in (7, 8, 9, 10, 11, 12)
    has_additional_terms = gibbs_eq_type_reduced in (4, 5, 6, 10, 11, 12)
    has_constant_Vm_terms = gibbs_eq_type_reduced in (2, 5, 8, 11)
    has_PTVm_terms = gibbs_eq_type_reduced in (3, 6, 9, 12)
    num_intervals = toks.parse(int)
    stoichiometry_pure_elements = toks.parseN(num_pure_elements, float)
    if has_constant_Vm_terms:
        raise ValueError("Constant molar volume equations (thermodynamic data options (2, 5, 8, 11)) are not supported yet.")
    # Piecewise endmember energy intervals
    if is_gibbs_energy_interval:
        intervals = [parse_interval_Gibbs(toks, num_gibbs_coeffs, has_additional_terms, has_PTVm_terms) for _ in range(num_intervals)]
    elif is_heat_capacity_interval:
        H298 = toks.parse(float)
        S298 = toks.parse(float)
        # parse the first without H_trans, then parse the rest
        intervals = [parse_interval_heat_capacity(toks, num_gibbs_coeffs, H298, S298, False, has_additional_terms, has_PTVm_terms)]
        for _ in range(num_intervals - 1):
            intervals.append(parse_interval_heat_capacity(toks, num_gibbs_coeffs, H298, S298, True, has_additional_terms, has_PTVm_terms))
    else:
        raise ValueError(f"Unknown thermodynamic data option type {gibbs_eq_type}. A number in [1, 24].")
    # magnetic terms
    if has_magnetic:
        curie_temperature = toks.parse(float)
        magnetic_moment = toks.parse(float)
        if is_stoichiometric:
            # two more terms
            # TODO: not clear what these are for, throwing them out for now.
            toks.parse(float)
            toks.parse(float)
        return EndmemberMagnetic(species_name, gibbs_eq_type, stoichiometry_pure_elements, intervals, curie_temperature, magnetic_moment)
    return Endmember(species_name, gibbs_eq_type, stoichiometry_pure_elements, intervals)


def parse_endmember_aqueous(toks: TokenParser, num_pure_elements: int, num_gibbs_coeffs: int):
    # add an extra "pure element" to parse the charge
    em = parse_endmember(toks, num_pure_elements + 1, num_gibbs_coeffs)
    return EndmemberAqueous(em.species_name, em.gibbs_eq_type, em.stoichiometry_pure_elements[1:], em.intervals, em.stoichiometry_pure_elements[0])


def parse_endmember_subq(toks: TokenParser, num_pure_elements, num_gibbs_coeffs, zeta=None):
    em = parse_endmember(toks, num_pure_elements, num_gibbs_coeffs)
    # TODO: is 5 correct? I only have two SUBQ/SUBG databases and they seem equivalent
    # I think the first four are the actual stoichiometries of each element in the quadruplet, but I'm unclear.
    stoichiometry_quadruplet = toks.parseN(5, float)
    if zeta is None:
        # This is SUBQ we need to parse it. If zeta is passed, that means we're in SUBG mode
        zeta = toks.parse(float)
    return EndmemberSUBQ(em.species_name, em.gibbs_eq_type, em.stoichiometry_pure_elements, em.intervals, stoichiometry_quadruplet, zeta)


def parse_quadruplet(toks):
    quad_idx = toks.parseN(4, int)
    quad_coords = toks.parseN(4, float)
    return Quadruplet(quad_idx, quad_coords)


def parse_subq_excess(toks, mixing_type, num_excess_coeffs):
    mixing_code = toks.parse(str)
    mixing_const = toks.parseN(4, int)
    mixing_exponents = toks.parseN(4, int)
    junk = toks.parseN(12, float)
    additional_mixing_const = toks.parse(int)
    additional_mixing_exponent = toks.parse(float)
    excess_coeffs = toks.parseN(num_excess_coeffs, float)
    return ExcessQuadruplet(mixing_type, mixing_code, mixing_const, mixing_exponents, junk, additional_mixing_const, additional_mixing_exponent, excess_coeffs)


def parse_phase_subq(toks, phase_name, phase_type, num_pure_elements, num_gibbs_coeffs, num_excess_coeffs):
    if phase_type == 'SUBG':
        zeta = toks.parse(float)
    else:
        zeta = None
    num_pairs = toks.parse(int)
    num_quadruplets = toks.parse(int)
    endmembers = [parse_endmember_subq(toks, num_pure_elements, num_gibbs_coeffs, zeta=zeta) for _ in range(num_pairs)]
    num_subl_1_const = toks.parse(int)
    num_subl_2_const = toks.parse(int)
    subl_1_const = toks.parseN(num_subl_1_const, str)
    subl_2_const = toks.parseN(num_subl_2_const, str)
    subl_1_charges = toks.parseN(num_subl_1_const, float)
    subl_1_chemical_groups = toks.parseN(num_subl_1_const, int)
    subl_2_charges = toks.parseN(num_subl_2_const, float)
    subl_2_chemical_groups = toks.parseN(num_subl_2_const, int)
    # TODO: not exactly sure my math is right on how many pairs, but I think it should be cations*anions
    subl_1_pair_idx = toks.parseN(num_subl_1_const*num_subl_2_const, int)
    subl_2_pair_idx = toks.parseN(num_subl_1_const*num_subl_2_const, int)
    subl_const_idx_pairs = [(s1i, s2i) for s1i, s2i in zip(subl_1_pair_idx, subl_2_pair_idx)]
    quadruplets = [parse_quadruplet(toks) for _ in range(num_quadruplets)]
    excess_parameters = []
    while True:
        mixing_type = toks.parse(int)
        if mixing_type == 0:
            break
        elif mixing_type == -9:
            # some garbage, like 1 2 3K 1 2K 1 3K 2 3 6, 90 of them
            toks.parseN(90, str)
            break
        excess_parameters.append(parse_subq_excess(toks, mixing_type, num_excess_coeffs))

    return Phase_SUBQ(phase_name, phase_type, endmembers, num_pairs, num_quadruplets, num_subl_1_const, num_subl_2_const, subl_1_const, subl_2_const, subl_1_charges, subl_1_chemical_groups, subl_2_charges, subl_2_chemical_groups, subl_const_idx_pairs, quadruplets, excess_parameters)


def parse_excess_magnetic_parameters(toks):
    excess_terms = []
    while True:
        num_interacting_species = toks.parse(int)
        if num_interacting_species == 0:
            break
        interacting_species_idxs = toks.parseN(num_interacting_species, int)
        num_terms = toks.parse(int)
        for parameter_order in range(num_terms):
            curie_temperature = toks.parse(float)
            magnetic_moment = toks.parse(float)
            excess_terms.append(ExcessCEFMagnetic(interacting_species_idxs, parameter_order, curie_temperature, magnetic_moment))
    return excess_terms


def parse_excess_parameters(toks, num_excess_coeffs):
    excess_terms = []
    while True:
        num_interacting_species = toks.parse(int)
        if num_interacting_species == 0:
            break
        interacting_species_idxs = toks.parseN(num_interacting_species, int)
        num_terms = toks.parse(int)
        for parameter_order in range(num_terms):
            excess_terms.append(ExcessCEF(interacting_species_idxs, parameter_order, toks.parseN(num_excess_coeffs, float)))
    return excess_terms


def parse_excess_parameters_pitz(toks, num_excess_coeffs):
    excess_terms = []
    while True:
        num_interacting_species = toks.parse(int)
        if num_interacting_species == 0:
            break
        # TODO: check if this is correct for this model
        # there are always 3 ints, regardless of the above "number of interacting species", if the number of interactings species is 2, we'll just throw the third number away for now
        if num_interacting_species == 2:
            interacting_species_idxs = toks.parseN(num_interacting_species, int)
            toks.parse(int)
        elif num_interacting_species == 3:
            interacting_species_idxs = toks.parseN(num_interacting_species, int)
        else:
            raise ValueError(f"Invalid number of interacting species for Pitzer model, got {num_interacting_species} (expected 2 or 3).")
        # TODO: not sure exactly if this value is parameter order, but it seems to be something like that
        parameter_order = None
        excess_terms.append(ExcessCEF(interacting_species_idxs, parameter_order, toks.parseN(num_excess_coeffs, float)))
    return excess_terms


def parse_excess_qkto(toks, num_excess_coeffs):
    excess_terms = []
    while True:
        num_interacting_species = toks.parse(int)
        if num_interacting_species == 0:
            break
        interacting_species_idxs = toks.parseN(num_interacting_species, int)
        interacing_species_type = toks.parseN(num_interacting_species, int)  # species type, for identify the asymmetry
        coefficients = toks.parseN(num_excess_coeffs, float)
        excess_terms.append(ExcessQKTO(interacting_species_idxs, interacing_species_type, coefficients))
    return excess_terms


def parse_phase_cef(toks, phase_name, phase_type, num_pure_elements, num_gibbs_coeffs, num_excess_coeffs, num_const):
    magnetic_phase_type = len(phase_type) == 5 and phase_type[4] == 'M'
    if magnetic_phase_type:
        # TODO: set the phase_type to phase_type[:4] so we don't need to add the
        # magnetic versions of the models all over the place. We also could then
        # raise if a model that does not support magnetism tries to add magnetism.
        magnetic_afm_factor = toks.parse(float)
        magnetic_structure_factor = toks.parse(float)
    else:
        magnetic_afm_factor = None
        magnetic_structure_factor = None

    endmembers = []
    for _ in range(num_const):
        if phase_type == 'PITZ':
            endmembers.append(parse_endmember_aqueous(toks, num_pure_elements, num_gibbs_coeffs))
        else:
            endmembers.append(parse_endmember(toks, num_pure_elements, num_gibbs_coeffs))
        if phase_type in ('QKTO',):
            # TODO: is this correct?
            # there's two additional numbers. they seem to always be 1.000 and 1, so we'll just throw them away
            toks.parse(float)
            toks.parse(int)

    # defining sublattice model
    if phase_type in ('SUBL', 'SUBLM'):
        num_subl = toks.parse(int)
        subl_atom_fracs = toks.parseN(num_subl, float)
        # some phases have number of atoms after a colon in the phase name, e.g. SIGMA:30
        if len(phase_name.split(':')) > 1:
            num_atoms = float(phase_name.split(':')[1])
        else:
            num_atoms = 1.0
        subl_ratios = [num_atoms*subl_frac for subl_frac in subl_atom_fracs]
        # read the data used to recover the mass, it's redundant and doesn't need to be stored
        subl_constituents = toks.parseN(num_subl, int)
        num_species = sum(subl_constituents)
        _ = toks.parseN(num_species, str)
        num_endmembers = int(np.prod(subl_constituents))
        for _ in range(num_subl):
            _ = toks.parseN(num_endmembers, int)
    elif phase_type in ('IDMX', 'RKMP', 'QKTO', 'RKMPM', 'PITZ'):
        subl_ratios = [1.0]
    else:
        raise NotImplemented(f"Phase type {phase_type} does not have method defined for determing the sublattice ratios")

    # excess terms
    if phase_type in ('IDMX',):
        # No excess parameters
        excess_parameters = []
    elif phase_type in ('PITZ',):
        excess_parameters = parse_excess_parameters_pitz(toks, num_excess_coeffs)
    elif phase_type in ('RKMP', 'SUBLM', 'RKMPM', 'SUBL'):
        # SUBL will have no excess parameters, but it will have the "0" terminator like it has excess parameters so we can use the excess parameter parsing to process it all the same.
        excess_parameters = []
        if magnetic_phase_type:
            excess_parameters.extend(parse_excess_magnetic_parameters(toks))
        excess_parameters.extend(parse_excess_parameters(toks, num_excess_coeffs))
    elif phase_type in ('QKTO',):
        excess_parameters = parse_excess_qkto(toks, num_excess_coeffs)
    return Phase_CEF(phase_name, phase_type, endmembers, subl_ratios, excess_parameters, magnetic_afm_factor=magnetic_afm_factor, magnetic_structure_factor=magnetic_structure_factor)


def parse_phase_real_gas(toks, phase_name, phase_type, num_pure_elements, num_gibbs_coeffs, num_const):
    endmembers = []
    for _ in range(num_const):
        em = parse_endmember(toks, num_pure_elements, num_gibbs_coeffs)
        Tc = toks.parse(float)
        Pc = toks.parse(float)
        Vc = toks.parse(float)
        acentric_factor = toks.parse(float)
        dipole_moment = toks.parse(float)
        endmembers.append(EndmemberRealGas(em.species_name, em.gibbs_eq_type, em.stoichiometry_pure_elements, em.intervals, Tc, Pc, Vc, acentric_factor, dipole_moment))
    return Phase_RealGas(phase_name, phase_type, endmembers)


def parse_phase_aqueous(toks, phase_name, phase_type, num_pure_elements, num_gibbs_coeffs, num_const):
    endmembers = [parse_endmember_aqueous(toks, num_pure_elements, num_gibbs_coeffs) for _ in range(num_const)]
    return Phase_Aqueous(phase_name, phase_type, endmembers)


def parse_phase(toks, num_pure_elements, num_gibbs_coeffs, num_excess_coeffs, num_const):
    """Dispatches to the correct parser depending on the phase type"""
    phase_name = toks.parse(str)
    phase_type = toks.parse(str)
    if phase_type in ('SUBQ', 'SUBG'):
        phase = parse_phase_subq(toks, phase_name, phase_type, num_pure_elements, num_gibbs_coeffs, num_excess_coeffs)
    elif phase_type == 'IDVD':
        phase = parse_phase_real_gas(toks, phase_name, phase_type, num_pure_elements, num_gibbs_coeffs, num_const)
    elif phase_type == 'IDWZ':
        phase = parse_phase_aqueous(toks, phase_name, phase_type, num_pure_elements, num_gibbs_coeffs, num_const)
    elif phase_type in ('IDMX', 'RKMP', 'RKMPM', 'QKTO', 'SUBL', 'SUBLM', 'PITZ'):
        # all these phases parse the same
        phase = parse_phase_cef(toks, phase_name, phase_type, num_pure_elements, num_gibbs_coeffs, num_excess_coeffs, num_const)
    else:
        raise NotImplementedError(f"phase type {phase_type} not yet supported")
    return phase


def parse_stoich_phase(toks, num_pure_elements, num_gibbs_coeffs):
    endmember = parse_endmember(toks, num_pure_elements, num_gibbs_coeffs, is_stoichiometric=True)
    phase_name = endmember.species_name
    return Phase_Stoichiometric(phase_name, None, [endmember])


def parse_cs_dat(instring):
    toks = tokenize(instring, startline=1)
    header = parse_header(toks)
    num_pure_elements = len(header.pure_elements)
    num_gibbs_coeffs = len(header.gibbs_coefficient_idxs)
    num_excess_coeffs = len(header.excess_coefficient_idxs)
    # num_const = 0 is gas phase that isn't present, so skip it
    solution_phases = [parse_phase(toks, num_pure_elements, num_gibbs_coeffs, num_excess_coeffs, num_const) for num_const in header.list_soln_species_count if num_const != 0]
    stoichiometric_phases = [parse_stoich_phase(toks, num_pure_elements, num_gibbs_coeffs) for _ in range(header.num_stoich_phases)]
    # Comment block at the end surrounded by "#####..." tokens
    # This might be a convention rather than required, but is an easy enough check to change.
    if len(toks) > 0:
        assert toks[0].startswith('#')
        assert toks[-1].startswith('#')
    return header, solution_phases, stoichiometric_phases, toks


def read_cs_dat(dbf: Database, fd):
    """
    Parse a ChemSage DAT file into a pycalphad Database object.

    Parameters
    ----------
    dbf : Database
        A pycalphad Database.
    fd : file-like
        File descriptor.
    """
    header, solution_phases, stoichiometric_phases, remaining_tokens = parse_cs_dat(fd.read().upper())
    # add elements and their reference states
    for el, mass in zip(header.pure_elements, header.pure_elements_mass):
        dbf.elements.add(el)
        dbf.species.add(v.Species(el))
        # add element reference state data
        dbf.refstates[el] = {
            'mass': mass,
            # the following metadata is not given in DAT files,
            # but is standard for our Database files
            'phase': None,
            'H298': None,
            'S298': None,
        }
    # Each phase subclass knows how to insert itself into the database.
    # The insert method will appropriately insert all endmembers as well.
    processed_phases = []
    for parsed_phase in (*solution_phases, *stoichiometric_phases):
        if parsed_phase.phase_name in processed_phases:
            # DAT files allow multiple entries of the same phase to handle
            # miscibility gaps. We discard the duplicate phase definitions.
            print(f"Skipping phase {parsed_phase.phase_name} because it's already in the database.")
            continue
        parsed_phase.insert(dbf, header.pure_elements, header.gibbs_coefficient_idxs, header.excess_coefficient_idxs)
        processed_phases.append(parsed_phase.phase_name)

    # process all the parameters that got added with dbf.add_parameter
    dbf.process_parameter_queue()

Database.register_format("dat", read=read_cs_dat, write=None)
