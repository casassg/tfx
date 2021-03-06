# Copyright 2021 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Pipeline state management functionality."""

import base64
from typing import List

from absl import logging
from tfx.orchestration import data_types_utils
from tfx.orchestration import metadata
from tfx.orchestration.experimental.core import status as status_lib
from tfx.orchestration.experimental.core import task as task_lib
from tfx.orchestration.portable.mlmd import context_lib
from tfx.orchestration.portable.mlmd import execution_lib
from tfx.proto.orchestration import pipeline_pb2

from ml_metadata.proto import metadata_store_pb2

_ORCHESTRATOR_RESERVED_ID = '__ORCHESTRATOR__'
_PIPELINE_IR = 'pipeline_ir'
_STOP_INITIATED = 'stop_initiated'
_NODE_STOP_INITIATED_PREFIX = 'node_stop_initiated_'
_ORCHESTRATOR_EXECUTION_TYPE = metadata_store_pb2.ExecutionType(
    name=_ORCHESTRATOR_RESERVED_ID,
    properties={_PIPELINE_IR: metadata_store_pb2.STRING})


class PipelineState:
  """Class for dealing with pipeline state. Can be used as a context manager."""

  def __init__(self,
               mlmd_handle: metadata.Metadata,
               pipeline_uid: task_lib.PipelineUid,
               context: metadata_store_pb2.Context,
               execution: metadata_store_pb2.Execution,
               commit: bool = False):
    """Constructor. Use one of the factory methods to initialize."""
    self.mlmd_handle = mlmd_handle
    self.pipeline_uid = pipeline_uid
    self.context = context
    self.execution = execution
    self._commit = commit
    self._pipeline = None  # lazily set

  @classmethod
  def new(cls, mlmd_handle: metadata.Metadata,
          pipeline: pipeline_pb2.Pipeline) -> 'PipelineState':
    """Creates a `PipelineState` object for a new pipeline.

    No active pipeline with the same pipeline uid should exist for the call to
    be successful.

    Args:
      mlmd_handle: A handle to the MLMD db.
      pipeline: IR of the pipeline.

    Returns:
      A `PipelineState` object.

    Raises:
      status_lib.StatusNotOkError: If a pipeline with same UID already exists.
    """
    pipeline_uid = task_lib.PipelineUid.from_pipeline(pipeline)
    context = context_lib.register_context_if_not_exists(
        mlmd_handle,
        context_type_name=_ORCHESTRATOR_RESERVED_ID,
        context_name=orchestrator_context_name(pipeline_uid))

    executions = mlmd_handle.store.get_executions_by_context(context.id)
    if any(e for e in executions if execution_lib.is_execution_active(e)):
      raise status_lib.StatusNotOkError(
          code=status_lib.Code.ALREADY_EXISTS,
          message=f'Pipeline with uid {pipeline_uid} already active.')

    execution = execution_lib.prepare_execution(
        mlmd_handle,
        _ORCHESTRATOR_EXECUTION_TYPE,
        metadata_store_pb2.Execution.NEW,
        exec_properties={
            _PIPELINE_IR:
                base64.b64encode(pipeline.SerializeToString()).decode('utf-8')
        })

    return cls(
        mlmd_handle=mlmd_handle,
        pipeline_uid=pipeline_uid,
        context=context,
        execution=execution,
        commit=True)

  @classmethod
  def load(cls, mlmd_handle: metadata.Metadata,
           pipeline_uid: task_lib.PipelineUid) -> 'PipelineState':
    """Loads pipeline state from MLMD.

    Args:
      mlmd_handle: A handle to the MLMD db.
      pipeline_uid: Uid of the pipeline state to load.

    Returns:
      A `PipelineState` object.

    Raises:
      status_lib.StatusNotOkError: With code=NOT_FOUND if no active pipeline
      with the given pipeline uid exists in MLMD. With code=INTERNAL if more
      than 1 active execution exists for given pipeline uid.
    """
    context = mlmd_handle.store.get_context_by_type_and_name(
        type_name=_ORCHESTRATOR_RESERVED_ID,
        context_name=orchestrator_context_name(pipeline_uid))
    if not context:
      raise status_lib.StatusNotOkError(
          code=status_lib.Code.NOT_FOUND,
          message=f'No active pipeline with uid {pipeline_uid} found.')
    return cls.load_from_orchestrator_context(mlmd_handle, context)

  @classmethod
  def load_from_orchestrator_context(
      cls, mlmd_handle: metadata.Metadata,
      context: metadata_store_pb2.Context) -> 'PipelineState':
    """Loads pipeline state for active pipeline under given orchestrator context.

    Args:
      mlmd_handle: A handle to the MLMD db.
      context: Pipeline context under which to find the pipeline execution.

    Returns:
      A `PipelineState` object.

    Raises:
      status_lib.StatusNotOkError: With code=NOT_FOUND if no active pipeline
      exists for the given context in MLMD. With code=INTERNAL if more than 1
      active execution exists for given pipeline uid.
    """
    pipeline_uid = pipeline_uid_from_orchestrator_context(context)
    active_executions = [
        e for e in mlmd_handle.store.get_executions_by_context(context.id)
        if execution_lib.is_execution_active(e)
    ]
    if not active_executions:
      raise status_lib.StatusNotOkError(
          code=status_lib.Code.NOT_FOUND,
          message=f'No active pipeline with uid {pipeline_uid} to load state.')
    if len(active_executions) > 1:
      raise status_lib.StatusNotOkError(
          code=status_lib.Code.INTERNAL,
          message=(
              f'Expected 1 but found {len(active_executions)} active pipeline '
              f'executions for pipeline uid: {pipeline_uid}'))

    return cls(
        mlmd_handle=mlmd_handle,
        pipeline_uid=pipeline_uid,
        context=context,
        execution=active_executions[0],
        commit=False)

  @property
  def pipeline(self) -> pipeline_pb2.Pipeline:
    if not self._pipeline:
      pipeline_ir_b64 = data_types_utils.get_metadata_value(
          self.execution.properties[_PIPELINE_IR])
      pipeline = pipeline_pb2.Pipeline()
      pipeline.ParseFromString(base64.b64decode(pipeline_ir_b64))
      self._pipeline = pipeline
    return self._pipeline

  def initiate_stop(self) -> None:
    """Updates pipeline state to signal stopping pipeline execution."""
    data_types_utils.set_metadata_value(
        self.execution.custom_properties[_STOP_INITIATED], 1)
    self._commit = True

  def is_stop_initiated(self) -> bool:
    """Returns `True` if pipeline execution stopping has been initiated."""
    if _STOP_INITIATED in self.execution.custom_properties:
      return data_types_utils.get_metadata_value(
          self.execution.custom_properties[_STOP_INITIATED]) == 1
    return False

  def initiate_node_start(self, node_uid: task_lib.NodeUid) -> None:
    """Updates pipeline state to signal that a node should be started."""
    if self.pipeline.execution_mode != pipeline_pb2.Pipeline.ASYNC:
      raise status_lib.StatusNotOkError(
          code=status_lib.Code.UNIMPLEMENTED,
          message='Node can be started only for async pipelines.')
    if not _is_node_uid_in_pipeline(node_uid, self.pipeline):
      raise status_lib.StatusNotOkError(
          code=status_lib.Code.INVALID_ARGUMENT,
          message=(f'Node given by uid {node_uid} does not belong to pipeline '
                   f'given by uid {self.pipeline_uid}'))
    property_name = _node_stop_initiated_property(node_uid)
    if property_name not in self.execution.custom_properties:
      return
    del self.execution.custom_properties[property_name]
    self._commit = True

  def initiate_node_stop(self, node_uid: task_lib.NodeUid) -> None:
    """Updates pipeline state to signal that a node should be stopped."""
    if self.pipeline.execution_mode != pipeline_pb2.Pipeline.ASYNC:
      raise status_lib.StatusNotOkError(
          code=status_lib.Code.UNIMPLEMENTED,
          message='Node can be started only for async pipelines.')
    if not _is_node_uid_in_pipeline(node_uid, self.pipeline):
      raise status_lib.StatusNotOkError(
          code=status_lib.Code.INVALID_ARGUMENT,
          message=(f'Node given by uid {node_uid} does not belong to pipeline '
                   f'given by uid {self.pipeline_uid}'))
    data_types_utils.set_metadata_value(
        self.execution.custom_properties[_node_stop_initiated_property(
            node_uid)], 1)
    self._commit = True

  def is_node_stop_initiated(self, node_uid: task_lib.NodeUid) -> bool:
    """Returns `True` if stopping has been initiated for the given node."""
    if node_uid.pipeline_uid != self.pipeline_uid:
      raise RuntimeError(
          f'Node given by uid {node_uid} does not belong to pipeline given '
          f'by uid {self.pipeline_uid}')
    property_name = _node_stop_initiated_property(node_uid)
    if property_name in self.execution.custom_properties:
      return data_types_utils.get_metadata_value(
          self.execution.custom_properties[property_name]) == 1
    return False

  def commit(self) -> None:
    """Commits pipeline state to MLMD if there are any mutations."""
    if self._commit:
      self.execution = execution_lib.put_execution(self.mlmd_handle,
                                                   self.execution,
                                                   [self.context])
      logging.info('Committed execution (id: %s) for pipeline with uid: %s',
                   self.execution.id, self.pipeline_uid)
    self._commit = False

  def __enter__(self) -> 'PipelineState':
    return self

  def __exit__(self, exc_type, exc_val, exc_tb):
    self.commit()


def get_orchestrator_contexts(
    mlmd_handle: metadata.Metadata) -> List[metadata_store_pb2.Context]:
  return mlmd_handle.store.get_contexts_by_type(_ORCHESTRATOR_RESERVED_ID)


# TODO(goutham): Handle sync pipelines.
def orchestrator_context_name(pipeline_uid: task_lib.PipelineUid) -> str:
  """Returns orchestrator reserved context name."""
  return f'{_ORCHESTRATOR_RESERVED_ID}_{pipeline_uid.pipeline_id}'


# TODO(goutham): Handle sync pipelines.
def pipeline_uid_from_orchestrator_context(
    context: metadata_store_pb2.Context) -> task_lib.PipelineUid:
  """Returns pipeline uid from orchestrator reserved context."""
  pipeline_id = context.name.split(_ORCHESTRATOR_RESERVED_ID + '_')[1]
  return task_lib.PipelineUid(pipeline_id=pipeline_id, pipeline_run_id=None)


def _node_stop_initiated_property(node_uid: task_lib.NodeUid) -> str:
  return f'{_NODE_STOP_INITIATED_PREFIX}{node_uid.node_id}'


def get_all_pipeline_nodes(
    pipeline: pipeline_pb2.Pipeline) -> List[pipeline_pb2.PipelineNode]:
  """Returns all pipeline nodes in the given pipeline."""
  result = []
  for pipeline_or_node in pipeline.nodes:
    which = pipeline_or_node.WhichOneof('node')
    # TODO(goutham): Handle sub-pipelines.
    # TODO(goutham): Handle system nodes.
    if which == 'pipeline_node':
      result.append(pipeline_or_node.pipeline_node)
    else:
      raise NotImplementedError('Only pipeline nodes supported.')
  return result


def _is_node_uid_in_pipeline(node_uid: task_lib.NodeUid,
                             pipeline: pipeline_pb2.Pipeline) -> bool:
  """Returns `True` if the `node_uid` belongs to the given pipeline."""
  for node in get_all_pipeline_nodes(pipeline):
    if task_lib.NodeUid.from_pipeline_node(pipeline, node) == node_uid:
      return True
  return False
