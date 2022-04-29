# Copyright 2022 OpenMined.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Privacy budget accounting for DP pipelines."""

import abc
import logging
import math
from typing import Optional

from dataclasses import dataclass
# TODO: import only modules https://google.github.io/styleguide/pyguide.html#22-imports
from pipeline_dp.aggregate_params import MechanismType

try:
    from dp_accounting import privacy_loss_distribution as pldlib
    from dp_accounting import common
except:
    # dp_accounting library is needed only for PLDBudgetAccountant which is
    # currently in experimental mode.
    pass


@dataclass
class MechanismSpec:
    """Specifies the parameters for a DP mechanism.

    MechanismType defines the kind of noise distribution.
    _noise_standard_deviation is the minimized noise standard deviation.
    (_eps, _delta) are parameters of (eps, delta)-differential privacy
    """
    mechanism_type: MechanismType
    _noise_standard_deviation: float = None
    _eps: float = None
    _delta: float = None
    _count: int = 1

    @property
    def noise_standard_deviation(self):
        """Noise value for the mechanism.

        Raises:
            AssertionError: The noise value is not calculated yet.
        """
        if self._noise_standard_deviation is None:
            raise AssertionError(
                "Noise standard deviation is not calculated yet.")
        return self._noise_standard_deviation

    @property
    def eps(self):
        """Parameter of (eps, delta)-differential privacy.
               Raises:
                   AssertionError: The privacy budget is not calculated yet.
       """
        if self._eps is None:
            raise AssertionError("Privacy budget is not calculated yet.")
        return self._eps

    @property
    def delta(self):
        """Parameter of (eps, delta)-differential privacy.
                Raises:
                    AssertionError: The privacy budget is not calculated yet.
        """
        if self._delta is None:
            raise AssertionError("Privacy budget is not calculated yet.")
        return self._delta

    @property
    def count(self):
        """The number of times the mechanism is going to be applied"""
        return self._count

    def set_eps_delta(self, eps: float, delta: Optional[float]) -> None:
        """Set parameters for (eps, delta)-differential privacy.

        Raises:
            AssertionError: eps must not be None.
        """
        if eps is None:
            raise AssertionError("eps must not be None.")
        self._eps = eps
        self._delta = delta
        return

    def use_delta(self) -> bool:
        return self.mechanism_type != MechanismType.LAPLACE


@dataclass
class MechanismSpecInternal:
    """Stores sensitivity and weight not exposed in MechanismSpec."""
    sensitivity: float
    weight: float
    mechanism_spec: MechanismSpec


class BudgetAccountant(abc.ABC):
    """Base class for budget accountants."""

    def __init__(self, n_aggregations: Optional[int],
                 aggregation_weights: Optional[list]):
        self._scopes_stack = []
        self._mechanisms = []
        self._finalized = False
        if n_aggregations is not None and aggregation_weights is not None:
            raise ValueError(
                "'n_aggregations' and 'aggregation_weights' can not be set simultaneously."
            )
        if n_aggregations is not None and n_aggregations <= 0:
            raise ValueError(
                f"'n_aggregations'={n_aggregations}, but it has to be positive."
            )
        self._n_aggregations = n_aggregations
        self._aggregation_weights = aggregation_weights
        self._next_aggregation_index = 0
        self._inside_aggregation_scope = False

    @abc.abstractmethod
    def request_budget(
            self,
            mechanism_type: MechanismType,
            sensitivity: float = 1,
            weight: float = 1,
            count: int = 1,
            noise_standard_deviation: Optional[float] = None) -> MechanismSpec:
        pass

    @abc.abstractmethod
    def compute_budgets(self):
        pass

    def scope(self,
              weight: float,
              aggregation_scope: bool = False) -> 'BudgetAccountantScope':
        """Returns a scope for DP operations

        The returned scope should consume no more than "weight" proportion of
        the budget of the parent scope.

        The accountant will automatically scale the budgets of all
        sub-operations accordingly.

        Example usage:
          with accountant.scope(weight = 0.5):
             ... some code that consumes DP budget ...

        Args:
            weight: budget weight of all operations made within this scope as
             compared to other scopes with the same parent scope.
            aggregation_scope: if True, this is an aggregation scope.
            Aggregation scopes are high-level scopes which corresponds to the
             whole aggregation computation graph (e.g. the call of
             DPEngine.aggregate()). Aggregation scopes can not include each
             other.

        Returns:
            the scope that should be used in a "with" block enclosing the
            operations consuming the budget.
        """
        aggregation_index = None
        if aggregation_scope and (self._n_aggregations or
                                  self._aggregation_weights):
            if self._inside_aggregation_scope:
                raise ValueError("Aggregation scopes can not be nested.")
            aggregation_index = self._get_next_aggregation_index()
            if self._n_aggregations:
                if aggregation_index >= self._n_aggregations:
                    raise ValueError("Exceeded the number of allowed "
                                     "aggregations. If you need more, update "
                                     "'n_aggregations' in the constructor of "
                                     "BudgetAccountant")
                if weight != 1:
                    raise ValueError(f"When 'n_aggregations' is set in the "
                                     f"constructor of BudgetAccountant, all "
                                     f"aggregation weights have to be 1, but "
                                     f"weight={weight}.")
            elif self._aggregation_weights != None:
                if aggregation_index >= len(self._aggregation_weights):
                    raise ValueError("Exceeded the number of allowed "
                                     "aggregations. If you need more, update "
                                     "'aggregation_weights' in the constructor "
                                     "of BudgetAccountant")
                expected_weight = self._aggregation_weights[aggregation_index]
                if weight != expected_weight:
                    raise ValueError(
                        f"The provided weight for the aggregation "
                        f"with index={aggregation_index} is {weight}, "
                        f"but 'aggregation_weights' in the constructor "
                        "of BudgetAccountant contains {expected_weight}")
            else:
                assert False, "It should not happen."
        return BudgetAccountantScope(self, weight, aggregation_index)

    def _register_mechanism(self, mechanism: MechanismSpecInternal):
        """Registers this mechanism for the future normalisation."""

        # Register in the global list of mechanisms
        self._mechanisms.append(mechanism)

        # Register in all of the current scopes
        for scope in self._scopes_stack:
            scope.mechanisms.append(mechanism)

        return mechanism

    def _enter_scope(self, scope):
        if scope.is_aggregation_scope:
            self._inside_aggregation_scope = True
        self._scopes_stack.append(scope)

    def _exit_scope(self):
        scope = self._scopes_stack.pop()
        if scope.is_aggregation_scope:
            self._inside_aggregation_scope = False

    def _finalize(self):
        self._check_number_aggregation_scopes()
        if self._finalized:
            raise Exception("compute_budgets can not be called twice.")
        self._finalized = True

    def _get_next_aggregation_index(self) -> int:
        self._next_aggregation_index += 1
        return self._next_aggregation_index - 1

    def _check_number_aggregation_scopes(self):
        # Check that number of created aggregation scopes is equal to the
        # expected numbers.
        aggregation_scopes = self._next_aggregation_index
        if self._n_aggregations and self._n_aggregations != aggregation_scopes:
            raise ValueError(f"In the constructor of BudgetAccountant "
                             f"'n_aggregations'={self._n_aggregations}, but "
                             f"actual = {aggregation_scopes}.")
        if self._aggregation_weights is not None:
            len_weights = len(self._aggregation_weights)
            if aggregation_scopes != len_weights:
                raise ValueError(
                    f"In the constructor of BudgetAccountant "
                    f"'len(aggregation_weights )'={len_weights}, but actual"
                    f"number of aggregations = {aggregation_scopes}.")


class BudgetAccountantScope:
    """The scope for the budget split.

    See the docstring to BudgetAccountant.scope() for more details.
    """

    def __init__(self,
                 accountant: BudgetAccountant,
                 weight: float,
                 index_aggregation_scope: Optional[int] = None):
        self.weight = weight
        self.accountant = accountant
        self._index_aggregation_scope = index_aggregation_scope
        self._epsilon = None
        self._delta = None
        if index_aggregation_scope is not None:
            self._compute_budget_for_aggregation_scope()
        self.mechanisms = []

    @property
    def epsilon(self):
        self._validate_epsilon_delta()
        return self._epsilon

    @property
    def delta(self):
        self._validate_epsilon_delta()
        return self._delta

    @property
    def is_aggregation_scope(self):
        return self._index_aggregation_scope is not None

    def _validate_epsilon_delta(self):
        if self._index_aggregation_scope is None:
            raise ValueError("Only aggregation scopes have computed budget.")
        if self._epsilon is None:
            raise ValueError("The budget per aggregation could not be computed."
                             " Please specify 'n_aggregations' or "
                             "'aggregation_weights' in Budget Accountant "
                             "constructor.")

    def __enter__(self):
        self.accountant._enter_scope(self)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.accountant._exit_scope()
        self._normalise_mechanism_weights()

    def _normalise_mechanism_weights(self):
        """Normalises all mechanism weights so that they sum up to the weight of the current scope."""

        if not self.mechanisms:
            return

        total_weight = sum([m.weight for m in self.mechanisms])
        normalisation_factor = self.weight / total_weight
        for mechanism in self.mechanisms:
            mechanism.weight *= normalisation_factor

    def _compute_budget_for_aggregation_scope(self):
        n_aggregations = self.accountant._n_aggregations
        weights = self.accountant._aggregation_weights
        if n_aggregations is not None:
            budget_ratio = 1 / n_aggregations
        elif weights is not None:
            sum_weights = sum(weights)
            budget_ratio = weights[self._index_aggregation_scope] / sum_weights
        else:
            # Restrictions on aggregations are not specified, do nothing.
            return
        self._epsilon = self.accountant._total_epsilon * budget_ratio
        self._delta = self.accountant._total_delta * budget_ratio


class NaiveBudgetAccountant(BudgetAccountant):
    """Manages the privacy budget."""

    def __init__(self,
                 total_epsilon: float,
                 total_delta: float,
                 n_aggregations: Optional[int] = None,
                 aggregation_weights: Optional[list] = None):
        """Constructs a NaiveBudgetAccountant.

        Args:
            total_epsilon: epsilon for the entire pipeline.
            total_delta: delta for the entire pipeline.
            n_aggregations: number of aggregations for which 'self' manages
              the budget. All aggregations should have budget_weight = 1.
              If None, any number of aggregations is allowed.
            aggregation_weights: weights of aggregations for which 'self'
              manages the budget. If None, any number of aggregations and
              weights are allowed.

        Raises:
            A ValueError if epsilon or delta are out of range.
            A ValueError if n_aggregations and aggregation_weights are both set.
        """
        super().__init__(n_aggregations, aggregation_weights)

        _validate_epsilon_delta(total_epsilon, total_delta)

        self._total_epsilon = total_epsilon
        self._total_delta = total_delta
        self._finalized = False

    def request_budget(
            self,
            mechanism_type: MechanismType,
            sensitivity: float = 1,
            weight: float = 1,
            count: int = 1,
            noise_standard_deviation: Optional[float] = None) -> MechanismSpec:
        """Requests a budget.

        Constructs a mechanism spec based on the parameters.
        Keeps the mechanism spec for future calculations.

        Args:
            mechanism_type: The type of noise distribution for the mechanism.
            sensitivity: The sensitivity for the mechanism.
            weight: The weight for the mechanism.
            count: The number of times the mechanism will be applied.
            noise_standard_deviation: The standard deviation for the mechanism.

        Returns:
            A "lazy" mechanism spec object that doesn't contain the noise
            standard deviation until compute_budgets is called.
        """
        if self._finalized:
            raise Exception(
                "request_budget() is called after compute_budgets(). "
                "Please ensure that compute_budgets() is called after DP "
                "aggregations.")

        if noise_standard_deviation is not None:
            raise NotImplementedError(
                "Count and noise standard deviation have not been implemented yet."
            )
        if mechanism_type == MechanismType.GAUSSIAN and self._total_delta == 0:
            raise ValueError(
                "The Gaussian mechanism requires that the pipeline delta is greater than 0"
            )
        mechanism_spec = MechanismSpec(mechanism_type=mechanism_type,
                                       _count=count)
        mechanism_spec_internal = MechanismSpecInternal(
            mechanism_spec=mechanism_spec,
            sensitivity=sensitivity,
            weight=weight)

        self._register_mechanism(mechanism_spec_internal)
        return mechanism_spec

    def compute_budgets(self):
        """Updates all previously requested MechanismSpec objects with corresponding budget values."""
        self._finalize()

        if not self._mechanisms:
            logging.warning("No budgets were requested.")
            return

        if self._scopes_stack:
            raise Exception(
                "Cannot call compute_budgets from within a budget scope.")

        total_weight_eps = total_weight_delta = 0
        for mechanism in self._mechanisms:
            total_weight_eps += mechanism.weight * mechanism.mechanism_spec.count
            if mechanism.mechanism_spec.use_delta():
                total_weight_delta += mechanism.weight * mechanism.mechanism_spec.count

        for mechanism in self._mechanisms:
            eps = delta = 0
            if total_weight_eps:
                numerator = self._total_epsilon * mechanism.weight
                eps = numerator / total_weight_eps
            if mechanism.mechanism_spec.use_delta():
                if total_weight_delta:
                    numerator = self._total_delta * mechanism.weight
                    delta = numerator / total_weight_delta
            mechanism.mechanism_spec.set_eps_delta(eps, delta)


class PLDBudgetAccountant(BudgetAccountant):
    """Manages the privacy budget for privacy loss distributions.

    It manages the privacy budget for the pipeline using the
    Privacy Loss Distribution (PLD) implementation from Google's
    dp_accounting library.

    This class is experimental. It is not yet compatible with DPEngine.
    """

    def __init__(self,
                 total_epsilon: float,
                 total_delta: float,
                 pld_discretization: float = 1e-4,
                 n_aggregations: Optional[int] = None,
                 aggregation_weights: Optional[list] = None):
        """Constructs a PLDBudgetAccountant.

        Args:
            total_epsilon: epsilon for the entire pipeline.
            total_delta: delta for the entire pipeline.
            pld_discretization: `value_discretization_interval` in PLD library.
                Smaller interval results in better accuracy, but increases running time.

        Raises:
            ValueError: Arguments are missing or out of range.
        """

        super().__init__(n_aggregations, aggregation_weights)

        _validate_epsilon_delta(total_epsilon, total_delta)

        self._total_epsilon = total_epsilon
        self._total_delta = total_delta
        self.minimum_noise_std = None
        self._pld_discretization = pld_discretization

    def request_budget(
            self,
            mechanism_type: MechanismType,
            sensitivity: float = 1,
            weight: float = 1,
            count: int = 1,
            noise_standard_deviation: Optional[float] = None) -> MechanismSpec:
        """Request a budget.

        Constructs a mechanism spec based on the parameters.
        Adds the mechanism to the pipeline for future calculation.

        Args:
            mechanism_type: The type of noise distribution for the mechanism.
            sensitivity: The sensitivity for the mechanism.
            weight: The weight for the mechanism.
            count: The number of times the mechanism will be applied.
            noise_standard_deviation: The standard deviation for the mechanism.


        Returns:
            A "lazy" mechanism spec object that doesn't contain the noise
            standard deviation until compute_budgets is called.
        """
        if self._finalized:
            raise Exception(
                "request_budget() is called after compute_budgets(). "
                "Please ensure that compute_budgets() is called after DP "
                "aggregations.")

        if count != 1 or noise_standard_deviation is not None:
            raise NotImplementedError(
                "Count and noise standard deviation have not been implemented yet."
            )
        if mechanism_type == MechanismType.GAUSSIAN and self._total_delta == 0:
            raise AssertionError(
                "The Gaussian mechanism requires that the pipeline delta is greater than 0"
            )
        mechanism_spec = MechanismSpec(mechanism_type=mechanism_type)
        mechanism_spec_internal = MechanismSpecInternal(
            mechanism_spec=mechanism_spec,
            sensitivity=sensitivity,
            weight=weight)
        self._register_mechanism(mechanism_spec_internal)
        return mechanism_spec

    def compute_budgets(self):
        """Computes the budget for the pipeline.

        Composes the mechanisms and adjusts the amount of
        noise based on given epsilon. Sets the noise for the
        entire pipeline.
        """
        self._finalize()

        if not self._mechanisms:
            logging.warning("No budgets were requested.")
            return

        if self._scopes_stack:
            raise Exception(
                "Cannot call compute_budgets from within a budget scope.")

        if self._total_delta == 0:
            sum_weights = 0
            for mechanism in self._mechanisms:
                sum_weights += mechanism.weight
            minimum_noise_std = sum_weights / self._total_epsilon * math.sqrt(2)
        else:
            minimum_noise_std = self._find_minimum_noise_std()

        self.minimum_noise_std = minimum_noise_std
        for mechanism in self._mechanisms:
            mechanism_noise_std = mechanism.sensitivity * minimum_noise_std / mechanism.weight
            mechanism.mechanism_spec._noise_standard_deviation = mechanism_noise_std
            if mechanism.mechanism_spec.mechanism_type == MechanismType.GENERIC:
                epsilon_0 = math.sqrt(2) / mechanism_noise_std
                delta_0 = epsilon_0 / self._total_epsilon * self._total_delta
                mechanism.mechanism_spec.set_eps_delta(epsilon_0, delta_0)

    def _find_minimum_noise_std(self) -> float:
        """Finds the minimum noise which satisfies the total budget.

        Use binary search to find a minimum noise value that gives a
        new epsilon close to the given epsilon (within a threshold).
        By increasing the noise we can decrease the epsilon.

        Returns:
            The noise value adjusted for the given epsilon.
        """
        threshold = 1e-4
        maximum_noise_std = self._calculate_max_noise_std()
        low, high = 0, maximum_noise_std
        while low + threshold < high:
            mid = (high - low) / 2 + low
            pld = self._compose_distributions(mid)
            pld_epsilon = pld.get_epsilon_for_delta(self._total_delta)
            if pld_epsilon <= self._total_epsilon:
                high = mid
            elif pld_epsilon > self._total_epsilon:
                low = mid

        return high

    def _calculate_max_noise_std(self) -> float:
        """Calculates an upper bound for the noise to satisfy the budget."""
        max_noise_std = 1
        pld_epsilon = self._total_epsilon + 1
        while pld_epsilon > self._total_epsilon:
            max_noise_std *= 2
            pld = self._compose_distributions(max_noise_std)
            pld_epsilon = pld.get_epsilon_for_delta(self._total_delta)
        return max_noise_std

    def _compose_distributions(
            self, noise_standard_deviation: float
    ) -> 'pldlib.PrivacyLossDistribution':
        """Uses the Privacy Loss Distribution library to compose distributions.

        Args:
            noise_standard_deviation: The noise of the distributions to construct.

        Returns:
            A PrivacyLossDistribution object for the pipeline.
        """
        composed, pld = None, None

        for mechanism_spec_internal in self._mechanisms:
            if mechanism_spec_internal.mechanism_spec.mechanism_type == MechanismType.LAPLACE:
                # The Laplace distribution parameter = std/sqrt(2).
                pld = pldlib.PrivacyLossDistribution.from_laplace_mechanism(
                    mechanism_spec_internal.sensitivity *
                    noise_standard_deviation / math.sqrt(2) /
                    mechanism_spec_internal.weight,
                    value_discretization_interval=self._pld_discretization)
            elif mechanism_spec_internal.mechanism_spec.mechanism_type == MechanismType.GAUSSIAN:
                pld = pldlib.PrivacyLossDistribution.from_gaussian_mechanism(
                    mechanism_spec_internal.sensitivity *
                    noise_standard_deviation / mechanism_spec_internal.weight,
                    value_discretization_interval=self._pld_discretization)
            elif mechanism_spec_internal.mechanism_spec.mechanism_type == MechanismType.GENERIC:
                # It is required to convert between the noise_standard_deviation of a Laplace or Gaussian mechanism
                # and the (epsilon, delta) Generic mechanism because the calibration is defined by one parameter.
                # There are multiple ways to do this; here it is assumed that (epsilon, delta) specifies the Laplace
                # mechanism and epsilon is computed based on this. The delta is computed to be proportional to epsilon.
                epsilon_0_interim = math.sqrt(2) / noise_standard_deviation
                delta_0_interim = epsilon_0_interim / self._total_epsilon * self._total_delta
                pld = pldlib.PrivacyLossDistribution.from_privacy_parameters(
                    common.DifferentialPrivacyParameters(
                        epsilon_0_interim, delta_0_interim),
                    value_discretization_interval=self._pld_discretization)

            composed = pld if composed is None else composed.compose(pld)

        return composed


def _validate_epsilon_delta(epsilon: float, delta: float):
    """Helper function to validate the epsilon and delta parameters.

    Args:
        epsilon: The epsilon value to validate.
        delta: The delta value to validate.

    Raises:
        A ValueError if either epsilon or delta are out of range.
    """
    if epsilon <= 0:
        raise ValueError(f"Epsilon must be positive, not {epsilon}.")
    if delta < 0:
        raise ValueError(f"Delta must be non-negative, not {delta}.")
