# Copyright 1999-2021 Alibaba Group Holding Ltd.
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

import weakref
from abc import abstractmethod
from typing import NamedTuple, Optional, Type, Set, Tuple, List

import numpy as np
from pandas.api.types import is_scalar

from .... import dataframe as md
from ....core import Tileable, get_output_types, ENTITY_TYPE, TileableGraph, Entity
from ....core.graph import EntityGraph
from ....dataframe.arithmetic.core import DataFrameUnaryUfunc, DataFrameBinopUfunc
from ....dataframe.base.eval import DataFrameEval
from ....dataframe.indexing.getitem import DataFrameIndex
from ....dataframe.indexing.setitem import DataFrameSetitem
from ....typing import OperandType, EntityType
from ....utils import implements
from ..core import OptimizationRecord, OptimizationRecordType, OptimizationRecords
from ..tileable.core import register_operand_based_optimization_rule
from .core import OperandBasedOptimizationRule


class EvalExtractRecord(NamedTuple):
    tileable: Optional[Tileable] = None
    expr: Optional[str] = None
    variables: Optional[dict] = None


def _get_binop_builder(op_str: str):
    def builder(lhs: str, rhs: str):
        return f"({lhs}) {op_str} ({rhs})"

    return builder


_func_name_to_builder = {
    "add": _get_binop_builder("+"),
    "sub": _get_binop_builder("-"),
    "mul": _get_binop_builder("*"),
    "floordiv": _get_binop_builder("//"),
    "truediv": _get_binop_builder("/"),
    "pow": _get_binop_builder("**"),
    "eq": _get_binop_builder("=="),
    "ne": _get_binop_builder("!="),
    "lt": _get_binop_builder("<"),
    "le": _get_binop_builder("<="),
    "gt": _get_binop_builder(">"),
    "ge": _get_binop_builder(">="),
    "__and__": _get_binop_builder("&"),
    "__or__": _get_binop_builder("|"),
    "__xor__": _get_binop_builder("^"),
    "negative": lambda expr: f"-({expr})",
    "__invert__": lambda expr: f"~({expr})",
}
_extract_result_cache = weakref.WeakKeyDictionary()


class _EvalRewriteOptimizationRule(OperandBasedOptimizationRule):
    def __init__(
        self,
        graph: EntityGraph,
        records: OptimizationRecords,
        optimizer_cls: Type["Optimizer"],
    ):
        super().__init__(graph, records, optimizer_cls)
        self._marked_nodes = set()

    def _mark_node(self, node: EntityType):
        self._marked_nodes.add(self._records.get_optimization_result(node, node))

    def _find_nodes_to_remove(self, node: EntityType) -> Set[EntityType]:
        node = self._records.get_optimization_result(node) or node
        removed_nodes = {node}
        results_set = set(self._graph.results)
        removed_pairs = []
        for pred in self._graph.iter_predecessors(node):
            pred_original = self._records.get_original_entity(pred, pred)
            pred_opt = self._records.get_optimization_result(pred, pred)

            if pred_opt in results_set or pred_original in results_set:
                continue

            if all(
                self._records.get_optimization_result(s, s) in self._marked_nodes
                for s in self._graph.iter_successors(pred)
            ):
                removed_pairs.append((pred_original, pred_opt))

        for pred_original, pred_opt in removed_pairs:
            removed_nodes.add(pred_opt)
            self._records.append_record(
                OptimizationRecord(pred_original, None, OptimizationRecordType.delete)
            )
        return removed_nodes

    def _replace_with_new_node(self, original_node: EntityType, new_node: EntityType):
        # Build the replaced subgraph
        subgraph = TileableGraph()
        subgraph.add_node(new_node)
        # Mark itself
        self._mark_node(original_node)
        # Find all the nodes to remove
        nodes_to_remove = self._find_nodes_to_remove(original_node)

        results_to_remove = list()
        new_results = list()
        if original_node in self._graph.results:
            results_to_remove = [original_node]
            new_results = [new_node]
        self._replace_subgraph(
            subgraph, nodes_to_remove, new_results, results_to_remove
        )
        self._records.append_record(
            OptimizationRecord(
                original_node,
                new_node,
                OptimizationRecordType.replace,
            )
        )

    @abstractmethod
    def apply_to_operand(self, op: OperandType) -> None:
        pass

    @abstractmethod
    def match_operand(self, op: OperandType) -> bool:
        pass


@register_operand_based_optimization_rule([DataFrameUnaryUfunc, DataFrameBinopUfunc])
class SeriesArithmeticToEval(_EvalRewriteOptimizationRule):
    _var_counter = 0

    @classmethod
    def _next_var_id(cls):
        cls._var_counter += 1
        return cls._var_counter

    @implements(OperandBasedOptimizationRule.match_operand)
    def match_operand(self, op: OperandType) -> bool:
        if op.gpu:
            return False
        _, expr, _ = self._extract_eval_expression(op.outputs[0])
        return expr is not None

    @staticmethod
    def _is_select_dataframe_column(tileable) -> bool:
        if not isinstance(tileable, md.Series) or not isinstance(
            tileable.op, DataFrameIndex
        ):
            return False

        input_df = tileable.inputs[0]
        index_op: DataFrameIndex = tileable.op
        if (
            not isinstance(input_df, md.DataFrame)
            or input_df.dtypes is None
            or not input_df.dtypes.index.is_unique
            or any(not isinstance(v, str) for v in input_df.dtypes.keys())
        ):
            return False

        return (
            isinstance(input_df, md.DataFrame)
            and input_df.dtypes is not None
            and index_op.col_names is not None
            and index_op.col_names in input_df.dtypes
            and index_op.mask is None
        )

    def _extract_eval_expression(self, tileable) -> EvalExtractRecord:
        if is_scalar(tileable):
            if isinstance(tileable, (int, bool, str, bytes, np.integer, np.bool_)):
                return EvalExtractRecord(expr=repr(tileable))
            else:
                var_name = f"__eval_scalar_var{self._next_var_id()}"
                var_dict = {var_name: tileable}
                return EvalExtractRecord(expr=f"@{var_name}", variables=var_dict)

        if not isinstance(tileable, ENTITY_TYPE):  # pragma: no cover
            return EvalExtractRecord()

        if tileable in _extract_result_cache:
            return _extract_result_cache[tileable]

        if self._is_select_dataframe_column(tileable):
            result = self._extract_column_select(tileable)
        elif isinstance(tileable.op, DataFrameUnaryUfunc):
            result = self._extract_unary(tileable)
        elif isinstance(tileable.op, DataFrameBinopUfunc):
            if tileable.op.fill_value is not None or tileable.op.level is not None:
                result = EvalExtractRecord()
            else:
                result = self._extract_binary(tileable)
        else:
            result = EvalExtractRecord()

        _extract_result_cache[tileable] = result
        return result

    @classmethod
    def _extract_column_select(cls, tileable) -> EvalExtractRecord:
        return EvalExtractRecord(tileable.inputs[0], f"`{tileable.op.col_names}`")

    def _extract_unary(self, tileable) -> EvalExtractRecord:
        op = tileable.op
        func_name = getattr(op, "_func_name") or getattr(op, "_bin_func_name")
        if func_name not in _func_name_to_builder:  # pragma: no cover
            return EvalExtractRecord()

        in_tileable, expr, variables = self._extract_eval_expression(op.inputs[0])
        if in_tileable is None:
            return EvalExtractRecord()

        self._mark_node(op.inputs[0])
        return EvalExtractRecord(
            in_tileable, _func_name_to_builder[func_name](expr), variables
        )

    def _extract_binary(self, tileable) -> EvalExtractRecord:
        op = tileable.op
        func_name = getattr(op, "_func_name", None) or getattr(op, "_bit_func_name")
        if func_name not in _func_name_to_builder:  # pragma: no cover
            return EvalExtractRecord()

        lhs_tileable, lhs_expr, lhs_vars = self._extract_eval_expression(op.lhs)
        if lhs_tileable is not None:
            self._mark_node(op.lhs)
        rhs_tileable, rhs_expr, rhs_vars = self._extract_eval_expression(op.rhs)
        if rhs_tileable is not None:
            self._mark_node(op.rhs)

        if lhs_expr is None or rhs_expr is None:
            return EvalExtractRecord()
        if (
            lhs_tileable is not None
            and rhs_tileable is not None
            and lhs_tileable.key != rhs_tileable.key
        ):
            return EvalExtractRecord()

        variables = (lhs_vars or dict()).copy()
        variables.update(rhs_vars or dict())
        in_tileable = next(t for t in [lhs_tileable, rhs_tileable] if t is not None)
        return EvalExtractRecord(
            in_tileable, _func_name_to_builder[func_name](lhs_expr, rhs_expr), variables
        )

    @implements(OperandBasedOptimizationRule.apply_to_operand)
    def apply_to_operand(self, op: OperandType):
        node = op.outputs[0]
        in_tileable, expr, variables = self._extract_eval_expression(node)
        opt_in_tileable = self._records.get_optimization_result(
            in_tileable, in_tileable
        )

        new_op = DataFrameEval(
            _key=node.op.key,
            _output_types=get_output_types(node),
            expr=expr,
            variables=variables or dict(),
            parser="pandas",
            is_query=False,
        )
        new_node = new_op.new_tileable(
            [opt_in_tileable], _key=node.key, _id=node.id, **node.params
        ).data
        self._replace_with_new_node(node, new_node)


class _DataFrameEvalRewriteRule(_EvalRewriteOptimizationRule):
    @implements(OperandBasedOptimizationRule.match_operand)
    def match_operand(self, op: OperandType) -> bool:
        optimized_eval_op = self._get_optimized_eval_op(op)
        if (
            op.gpu
            or not isinstance(optimized_eval_op, DataFrameEval)
            or optimized_eval_op.is_query
            or optimized_eval_op.inputs[0].key != op.inputs[0].key
        ):
            return False
        return True

    def _build_new_eval_op(self, op: OperandType):
        raise NotImplementedError

    def _get_optimized_eval_op(self, op: OperandType) -> OperandType:
        in_columnar_node = self._get_input_columnar_node(op)
        optimized = self._records.get_optimization_result(in_columnar_node)
        return optimized.op if optimized is not None else in_columnar_node.op

    def _get_input_columnar_node(self, op: OperandType) -> ENTITY_TYPE:
        raise NotImplementedError

    @implements(OperandBasedOptimizationRule.apply_to_operand)
    def apply_to_operand(self, op: DataFrameIndex):
        node = op.outputs[0]
        in_tileable = op.inputs[0]
        in_columnar_node = self._get_input_columnar_node(op)
        opt_in_tileable = self._records.get_optimization_result(
            in_tileable, in_tileable
        )

        new_op = self._build_new_eval_op(op)
        new_node = new_op.new_tileable(
            [opt_in_tileable], _key=node.key, _id=node.id, **node.params
        ).data
        self._mark_node(in_columnar_node)
        self._replace_with_new_node(node, new_node)


@register_operand_based_optimization_rule([DataFrameIndex])
class DataFrameBoolEvalToQuery(_DataFrameEvalRewriteRule):
    @implements(OperandBasedOptimizationRule.match_operand)
    def match_operand(self, op: DataFrameIndex) -> bool:
        if (
            op.col_names is not None
            or not isinstance(op.mask, md.Series)
            or op.mask.dtype != bool
        ):
            return False
        return super().match_operand(op)

    def _get_input_columnar_node(self, op: OperandType) -> ENTITY_TYPE:
        return op.mask

    def _build_new_eval_op(self, op: OperandType):
        in_eval_op = self._get_optimized_eval_op(op)
        return DataFrameEval(
            _key=op.key,
            _output_types=get_output_types(op.outputs[0]),
            expr=in_eval_op.expr,
            variables=in_eval_op.variables,
            parser="pandas",
            is_query=True,
        )


@register_operand_based_optimization_rule([DataFrameSetitem])
class DataFrameEvalSetItemToEval(_DataFrameEvalRewriteRule):
    @implements(OperandBasedOptimizationRule.match_operand)
    def match_operand(self, op: DataFrameSetitem):
        if not isinstance(op.indexes, str) or not isinstance(op.value, md.Series):
            return False
        return super().match_operand(op)

    def _get_input_columnar_node(self, op: DataFrameSetitem) -> ENTITY_TYPE:
        return op.value

    def _build_new_eval_op(self, op: DataFrameSetitem):
        in_eval_op = self._get_optimized_eval_op(op)
        return DataFrameEval(
            _key=op.key,
            _output_types=get_output_types(op.outputs[0]),
            expr=f"`{op.indexes}` = {in_eval_op.expr}",
            variables=in_eval_op.variables,
            parser="pandas",
            is_query=False,
            self_target=True,
        )

    @implements(OperandBasedOptimizationRule.apply_to_operand)
    def apply_to_operand(self, op: DataFrameIndex):
        super().apply_to_operand(op)

        node = op.outputs[0]
        opt_node = self._records.get_optimization_result(node, node)
        if not isinstance(opt_node.op, DataFrameEval):  # pragma: no cover
            return

        # when encountering consecutive SetItems, expressions can be
        # merged as a multiline expression
        pred_opt_node = opt_node.inputs[0]
        if (
            isinstance(pred_opt_node.op, DataFrameEval)
            and opt_node.op.parser == pred_opt_node.op.parser == "pandas"
            and not opt_node.op.is_query
            and not pred_opt_node.op.is_query
            and opt_node.op.self_target
            and pred_opt_node.op.self_target
        ):
            new_expr = pred_opt_node.op.expr + "\n" + opt_node.op.expr
            new_variables = (pred_opt_node.op.variables or dict()).copy()
            new_variables.update(opt_node.op.variables or dict())

            new_op = DataFrameEval(
                _key=op.key,
                _output_types=get_output_types(op.outputs[0]),
                expr=new_expr,
                variables=new_variables,
                parser="pandas",
                is_query=False,
                self_target=True,
            )
            new_node = new_op.new_tileable(
                pred_opt_node.inputs, _key=node.key, _id=node.id, **node.params
            ).data
            self._mark_node(pred_opt_node)
            self._replace_with_new_node(opt_node, new_node)
