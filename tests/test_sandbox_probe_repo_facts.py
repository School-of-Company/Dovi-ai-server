from pathlib import Path

from app.sandbox_probe.runner import read_repo_facts


def test_read_repo_facts_ignores_symlinks_that_point_outside_the_repo(tmp_path: Path) -> None:
    host_file = tmp_path / "host-file.env"
    host_file.write_text("WORKER_TOKEN=ghs_hostsecret\n")
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text("{}")
    (repo / ".env.example").symlink_to(host_file)
    (repo / "docker-compose.yml").symlink_to(host_file)
    (repo / "src" / "leak.ts").symlink_to(host_file)
    (repo / "src" / "ok.ts").write_text("process.env.PORT")

    facts = read_repo_facts(repo)

    assert facts.env_example == ""
    assert facts.compose == ""
    assert facts.sources == ["process.env.PORT"]
    assert "ghs_hostsecret" not in repr(facts)


def test_read_repo_facts_ignores_symlinks_even_when_they_stay_inside_the_repo(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "real.env").write_text("PORT=3000\n")
    (repo / ".env.example").symlink_to(repo / "real.env")

    assert read_repo_facts(repo).env_example == ""


def test_read_repo_facts_reads_regular_files_inside_the_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text('{"name": "x"}')
    (repo / ".env.example").write_text("PORT=3000\n")
    (repo / "pnpm-lock.yaml").write_text("")
    (repo / ".nvmrc").write_text("22\n")

    facts = read_repo_facts(repo)

    assert facts.package_json == '{"name": "x"}'
    assert facts.env_example == "PORT=3000\n"
    assert facts.lockfiles == ["pnpm-lock.yaml"]
    assert facts.node_version_files == ["22\n"]
