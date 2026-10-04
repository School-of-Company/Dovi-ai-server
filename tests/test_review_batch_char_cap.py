from app.review.diff import analyze
from app.review.pipeline import (
    _MAX_DIFF_FILE_CHARS,
    _MAX_DIFF_TOTAL_CHARS,
    ReviewPipeline,
    _PromptReport,
    _truncate_diff_blocks,
    _truncate_diff_blocks_detailed,
)
from app.review.schema import (
    ChangedFile,
    ReviewCompletedEvent,
    ReviewModelOutput,
    ReviewRequestedEvent,
    ReviewTarget,
    make_review_job_id,
)
from tests.test_review_pipeline import FakeLLM


def _patch(lines: int, width: int = 60) -> str:
    body = "\n".join("+" + "x" * width for _ in range(lines))
    return f"@@ -0,0 +1,{lines} @@\n{body}"


def _event(patches: dict[str, str], pr_body: str = "") -> ReviewRequestedEvent:
    return ReviewRequestedEvent(
        review_job_id=make_review_job_id(42, 7, "abc123"),
        repository_id=42,
        pr_number=7,
        head_sha="abc123",
        base_sha="def456",
        pr_body=pr_body,
        changed_files=[
            ChangedFile(file_path=path, status="modified", patch=patch)
            for path, patch in patches.items()
        ],
    )


def _pipeline(fake: FakeLLM) -> ReviewPipeline:
    return ReviewPipeline(fake, model_version="v", prompt_version="v1")


def _outputs(count: int) -> list[ReviewModelOutput | Exception]:
    return [ReviewModelOutput(summary=f"요약 {i}", reviews=[]) for i in range(count)]


async def test_files_that_fit_the_token_budget_but_not_the_char_cap_are_split_not_dropped() -> None:
    # 파일당 약 6.3k자 x 5개 = 31k자 > 20k자 상한. 토큰은 FakeLLM 기본값(아주 작음)이라
    # 예전에는 한 배치에 다 묶여 뒤쪽 파일이 조용히 빠졌다.
    patches = {f"src/f{i}.py": _patch(100) for i in range(5)}
    fake = FakeLLM(sequence=_outputs(2))

    result = await _pipeline(fake).run(_event(patches))

    assert isinstance(result, ReviewCompletedEvent)
    assert fake.call_count == 2
    prompts = [messages[1]["content"] for messages, _, _ in fake.generate_calls]
    assert all("크기 제한으로 생략된 파일" not in prompt for prompt in prompts)
    for path in patches:
        assert sum(path in prompt for prompt in prompts) == 1
    assert "리뷰하지 못한 파일" not in result.summary
    assert "일부만 리뷰된 파일" not in result.summary


async def test_batches_stay_within_the_diff_char_cap() -> None:
    patches = {f"src/f{i}.py": _patch(100) for i in range(5)}
    pipeline = _pipeline(FakeLLM(sequence=_outputs(2)))
    event = _event(patches)
    targets = analyze(event)

    batches, omitted = await pipeline._split_targets_into_batches(event, targets, {}, "", "")

    assert [len(batch) for batch in batches] == [3, 2]
    assert omitted == []
    for batch in batches:
        rendered = sum(len(pipeline._render_target(t, [])) for t in batch)
        assert rendered <= _MAX_DIFF_TOTAL_CHARS


async def test_first_batch_leaves_room_for_the_pr_description_in_the_char_cap() -> None:
    patches = {f"src/f{i}.py": _patch(100) for i in range(3)}
    plain_event = _event(patches)
    described_event = _event(patches, pr_body="설명 " * 600)
    pipeline = _pipeline(FakeLLM())

    plain, _ = await pipeline._split_targets_into_batches(
        plain_event, analyze(plain_event), {}, "", ""
    )
    described, _ = await pipeline._split_targets_into_batches(
        described_event, analyze(described_event), {}, "", ""
    )

    assert [len(b) for b in plain] == [3]
    assert [len(b) for b in described] == [2, 1]


async def test_large_file_is_split_into_pieces_instead_of_truncated() -> None:
    # 파일 하나가 파일당 상한(8k자)을 넘어도 뒷부분을 버리지 않고 조각으로 나눠 전부 리뷰한다.
    patches = {"src/big.py": _patch(200), "src/small.py": _patch(5)}
    fake = FakeLLM(sequence=_outputs(3))

    result = await _pipeline(fake).run(_event(patches))

    assert isinstance(result, ReviewCompletedEvent)
    prompts = [call[0][1]["content"] for call in fake.generate_calls]
    assert all("...(truncated)" not in prompt for prompt in prompts)
    assert any("src/big.py" in prompt for prompt in prompts)
    assert "일부만 리뷰된 파일" not in result.summary
    assert "리뷰하지 못한 파일" not in result.summary


async def test_files_dropped_while_assembling_the_prompt_are_reported_by_us() -> None:
    patches = {f"src/f{i}.py": _patch(100) for i in range(5)}
    event = _event(patches)
    fake = FakeLLM(output=ReviewModelOutput(summary="요약", reviews=[]))
    pipeline = _pipeline(fake)
    targets = analyze(event)

    async def one_batch(
        *args: object, **kwargs: object
    ) -> tuple[list[list[ReviewTarget]], list[str]]:
        return [targets], []

    pipeline._split_targets_into_batches = one_batch  # type: ignore[method-assign]

    result = await pipeline.run(event)

    assert isinstance(result, ReviewCompletedEvent)
    prompt = fake.generate_calls[0][0][1]["content"]
    assert "크기 제한으로 생략된 파일 1개: src/f4.py" in prompt
    assert "(리뷰하지 못한 파일 1개: src/f4.py)" in result.summary
    assert "(일부만 리뷰된 파일 1개: src/f3.py)" in result.summary


async def test_batch_failure_and_prompt_drop_do_not_list_a_file_twice() -> None:
    patches = {f"src/f{i}.py": _patch(100) for i in range(5)}
    event = _event(patches)
    pipeline = _pipeline(FakeLLM(output=ReviewModelOutput(summary="요약", reviews=[])))
    targets = analyze(event)

    async def one_batch(
        *args: object, **kwargs: object
    ) -> tuple[list[list[ReviewTarget]], list[str]]:
        return [targets], ["src/f4.py"]

    pipeline._split_targets_into_batches = one_batch  # type: ignore[method-assign]

    result = await pipeline.run(event)

    assert isinstance(result, ReviewCompletedEvent)
    assert "(리뷰하지 못한 파일 1개: src/f4.py)" in result.summary
    assert result.summary.count("src/f4.py") == 1


def test_build_messages_fills_the_prompt_report() -> None:
    patches = {"src/big.py": _patch(200), **{f"src/f{i}.py": _patch(100) for i in range(4)}}
    event = _event(patches)
    pipeline = _pipeline(FakeLLM())
    report = _PromptReport()

    pipeline._build_messages(event, analyze(event), {}, report=report)

    assert report.truncated_files == ["src/big.py", "src/f1.py"]
    assert report.dropped_files == ["src/f2.py", "src/f3.py"]


def test_truncate_diff_blocks_detailed_reports_dropped_and_truncated_paths() -> None:
    blocks = [
        ("a.py", "a" * (_MAX_DIFF_FILE_CHARS + 500)),
        ("b.py", "b" * 9000),
        ("c.py", "c" * 9000),
        ("d.py", "d" * 100),
    ]

    diff, dropped, truncated = _truncate_diff_blocks_detailed(blocks)

    assert truncated == ["a.py", "b.py", "c.py"]
    assert dropped == ["d.py"]
    assert "(크기 제한으로 생략된 파일 1개: d.py" in diff
    assert diff == _truncate_diff_blocks(blocks)


def test_file_left_with_only_its_header_counts_as_dropped_not_partial() -> None:
    blocks = [
        ("a.py", "# a.py (modified)\n" + "a" * (_MAX_DIFF_TOTAL_CHARS - 40)),
        ("b.py", "# b.py (modified)\n" + "b" * 500),
    ]

    diff, dropped, truncated = _truncate_diff_blocks_detailed(
        blocks, max_file_chars=_MAX_DIFF_TOTAL_CHARS
    )

    assert dropped == ["b.py"]
    assert truncated == []
    assert "# b.py (modified)\n...(truncated)" not in diff


def test_truncate_diff_blocks_detailed_is_empty_for_small_diffs() -> None:
    diff, dropped, truncated = _truncate_diff_blocks_detailed([("a.py", "small")])

    assert (diff, dropped, truncated) == ("small", [], [])


async def test_file_with_only_some_pieces_reviewed_is_reported_as_partial() -> None:
    # 큰 파일이 조각 여러 개로 나뉘었는데 배치 상한 때문에 일부 조각만 리뷰된 경우,
    # "리뷰하지 못한 파일"이 아니라 "일부만 리뷰된 파일"로 안내해야 한다.
    fake = FakeLLM(sequence=_outputs(2))
    pipeline = ReviewPipeline(
        fake, model_version="v", prompt_version="v1", max_review_batches=2
    )

    result = await pipeline.run(_event({"src/huge.py": _patch(600)}))

    assert isinstance(result, ReviewCompletedEvent)
    assert "(일부만 리뷰된 파일 1개: src/huge.py)" in result.summary
    assert "리뷰하지 못한 파일" not in result.summary
