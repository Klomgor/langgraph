from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Sequence,
    TypeVar,
    Union,
    cast,
)
from uuid import UUID, uuid4

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGenerationChunk, LLMResult

from langgraph.constants import NS_SEP, TAG_HIDDEN, TAG_NOSTREAM
from langgraph.types import Command, StreamChunk

try:
    from langchain_core.tracers._streaming import _StreamingCallbackHandler
except ImportError:
    _StreamingCallbackHandler = object  # type: ignore

T = TypeVar("T")
Meta = tuple[tuple[str, ...], dict[str, Any]]


class StreamMessagesHandler(BaseCallbackHandler, _StreamingCallbackHandler):
    """A callback handler that implements stream_mode=messages.
    Collects messages from (1) chat model stream events and (2) node outputs."""

    run_inline = True
    """We want this callback to run in the main thread, to avoid order/locking issues."""

    def __init__(self, stream: Callable[[StreamChunk], None]):
        self.stream = stream
        self.metadata: dict[UUID, Meta] = {}
        self.seen: set[Union[int, str]] = set()

    def _emit(self, meta: Meta, message: BaseMessage, *, dedupe: bool = False) -> None:
        if dedupe and message.id in self.seen:
            return
        else:
            if message.id is None:
                message.id = str(uuid4())
            self.seen.add(message.id)
            self.stream((meta[0], "messages", (message, meta[1])))

    def tap_output_aiter(
        self, run_id: UUID, output: AsyncIterator[T]
    ) -> AsyncIterator[T]:
        return output

    def tap_output_iter(self, run_id: UUID, output: Iterator[T]) -> Iterator[T]:
        return output

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        if metadata and (not tags or TAG_NOSTREAM not in tags):
            self.metadata[run_id] = (
                tuple(cast(str, metadata["langgraph_checkpoint_ns"]).split(NS_SEP)),
                metadata,
            )

    def on_llm_new_token(
        self,
        token: str,
        *,
        chunk: Optional[ChatGenerationChunk] = None,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> Any:
        if not isinstance(chunk, ChatGenerationChunk):
            return
        if meta := self.metadata.get(run_id):
            filtered_tags = [t for t in (tags or []) if not t.startswith("seq:step")]
            if filtered_tags:
                meta[1]["tags"] = filtered_tags
            self._emit(meta, chunk.message)

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        self.metadata.pop(run_id, None)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        self.metadata.pop(run_id, None)

    def on_chain_start(
        self,
        serialized: Dict[str, Any],
        inputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        if (
            metadata
            and kwargs.get("name") == metadata.get("langgraph_node")
            and (not tags or TAG_HIDDEN not in tags)
        ):
            self.metadata[run_id] = (
                tuple(cast(str, metadata["langgraph_checkpoint_ns"]).split(NS_SEP)),
                metadata,
            )
            if isinstance(inputs, dict):
                for key, value in inputs.items():
                    if isinstance(value, BaseMessage):
                        if value.id is not None:
                            self.seen.add(value.id)
                    elif isinstance(value, Sequence) and not isinstance(value, str):
                        for item in value:
                            if isinstance(item, BaseMessage):
                                if item.id is not None:
                                    self.seen.add(item.id)

    def on_chain_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        if meta := self.metadata.pop(run_id, None):
            self._process_response(response, meta, 0)

    def _process_response(self, response: Any, meta: Meta, depth: int = 0) -> None:
        """Recursively process a response to find and emit BaseMessage instances."""
        if response is None:
            return

        # Cap recursion depth at 5
        if depth >= 5:
            return

        if isinstance(response, BaseMessage):
            self._emit(meta, response, dedupe=True)
        elif isinstance(response, dict):
            for value in response.values():
                self._process_response(value, meta, depth + 1)
        elif isinstance(response, Sequence) and not isinstance(response, str):
            for item in response:
                self._process_response(item, meta, depth + 1)
        elif hasattr(response, "__dir__") and callable(response.__dir__):
            for key in dir(response):
                # Skip magic methods and properties to reduce recursion depth
                if key.startswith("__") and key.endswith("__"):
                    continue
                try:
                    value = getattr(response, key)
                    self._process_response(value, meta, depth + 1)
                except AttributeError:
                    pass
        elif isinstance(response, Command):
            self._process_response(response.update, meta, depth + 1)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        self.metadata.pop(run_id, None)
