from collections import defaultdict

from titanium.job import Job
from titanium.models.job.config import JobConfig


def test_resume_cleanup_preserves_critiques_metadata_dir(tmp_path):
    config = JobConfig(job_name="job", jobs_dir=tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "config.json").write_text(config.model_dump_json(), encoding="utf-8")

    critiques_dir = job_dir / ".critiques" / "olympus-qa-v3"
    critiques_dir.mkdir(parents=True)
    (critiques_dir / "result.json").write_text("{}", encoding="utf-8")

    incomplete_trial_dir = job_dir / "task__abc123"
    incomplete_trial_dir.mkdir()
    (incomplete_trial_dir / "config.json").write_text("{}", encoding="utf-8")

    job = Job(config, _task_configs=[], _metrics=defaultdict(list))
    job._close_logger_handlers()

    assert (job_dir / ".critiques").is_dir()
    assert critiques_dir.is_dir()
    assert not incomplete_trial_dir.exists()

