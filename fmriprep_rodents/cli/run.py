#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""fMRI preprocessing workflow."""
from .. import config


def main():
    """Entry point."""
    from os import EX_SOFTWARE
    from pathlib import Path
    import sys
    import gc
    from multiprocessing import Process, Manager
    from .parser import parse_args
    from ..utils.bids import write_derivative_description

    parse_args()

    sentry_sdk = None
    if not config.execution.notrack:
        import sentry_sdk
        from ..utils.sentry import sentry_setup

        sentry_setup()

    # CRITICAL Save the config to a file. This is necessary because the execution graph
    # is built as a separate process to keep the memory footprint low. The most
    # straightforward way to communicate with the child process is via the filesystem.
    config_file = config.execution.work_dir / f"config-{config.execution.run_uuid}.toml"
    config.to_filename(config_file)

    # CRITICAL Call build_workflow(config_file, retval) in a subprocess.
    # Because Python on Linux does not ever free virtual memory (VM), running the
    # workflow construction jailed within a process preempts excessive VM buildup.
    with Manager() as mgr:
        from .workflow import build_workflow

        retval = mgr.dict()
        p = Process(target=build_workflow, args=(str(config_file), retval))
        p.start()
        p.join()

        retcode = p.exitcode or retval.get("return_code", 0)
        fmriprep_wf = retval.get("workflow", None)

    # CRITICAL Load the config from the file. This is necessary because the ``build_workflow``
    # function executed constrained in a process may change the config (and thus the global
    # state of fMRIPrep).
    config.load(config_file)

    if config.execution.reports_only:
        sys.exit(int(retcode > 0))

    if fmriprep_wf and config.execution.write_graph:
        fmriprep_wf.write_graph(graph2use="colored", format="svg", simple_form=True)

    retcode = retcode or (fmriprep_wf is None) * EX_SOFTWARE
    if retcode != 0:
        sys.exit(retcode)

    # Generate boilerplate
    with Manager() as mgr:
        from .workflow import build_boilerplate

        p = Process(target=build_boilerplate, args=(str(config_file), fmriprep_wf))
        p.start()
        p.join()

    if config.execution.boilerplate_only:
        sys.exit(int(retcode > 0))

    # Clean up master process before running workflow, which may create forks
    gc.collect()

    # Sentry tracking
    if sentry_sdk is not None:
        with sentry_sdk.configure_scope() as scope:
            scope.set_tag("run_uuid", config.execution.run_uuid)
            scope.set_tag("npart", len(config.execution.participant_label))
        sentry_sdk.add_breadcrumb(message="fMRIPrep started", level="info")
        sentry_sdk.capture_message("fMRIPrep started", level="info")

    config.loggers.workflow.log(
        15,
        "\n".join(
            ["fMRIPrep config:"] + ["\t\t%s" % s for s in config.dumps().splitlines()]
        ),
    )
    config.loggers.workflow.log(25, "fMRIPrep started!")
    errno = 1  # Default is error exit unless otherwise set
    try:
        fmriprep_wf.run(**config.nipype.get_plugin())
    except Exception as e:
        if not config.execution.notrack:
            from ..utils.sentry import process_crashfile

            crashfolders = [
                config.execution.output_dir
                / "fmriprep-rodents"
                / "sub-{}".format(s)
                / "log"
                / config.execution.run_uuid
                for s in config.execution.participant_label
            ]
            for crashfolder in crashfolders:
                for crashfile in crashfolder.glob("crash*.*"):
                    process_crashfile(crashfile)

            if "Workflow did not execute cleanly" not in str(e):
                sentry_sdk.capture_exception(e)
        config.loggers.workflow.critical("fMRIPrep failed: %s", e)
        raise
    else:
        config.loggers.workflow.log(25, "fMRIPrep finished successfully!")
        if not config.execution.notrack:
            success_message = "fMRIPrep finished without errors"
            sentry_sdk.add_breadcrumb(message=success_message, level="info")
            sentry_sdk.capture_message(success_message, level="info")

        # Bother users with the boilerplate only iff the workflow went okay.
        boiler_file = config.execution.output_dir / "fmriprep-rodents" / "logs" / "CITATION.md"
        if boiler_file.exists():
            if config.environment.exec_env in (
                "singularity",
                "docker",
                "fmriprep-docker",
            ):
                boiler_file = Path("<OUTPUT_PATH>") / boiler_file.relative_to(
                    config.execution.output_dir
                )
            config.loggers.workflow.log(
                25,
                "Works derived from this fMRIPrep execution should include the "
                f"boilerplate text found in {boiler_file}.",
            )

        if config.workflow.run_reconall:
            from templateflow import api
            from niworkflows.utils.misc import _copy_any

            dseg_tsv = str(api.get("fsaverage", suffix="dseg", extension=[".tsv"]))
            _copy_any(
                dseg_tsv,
                str(config.execution.output_dir / "fmriprep-rodents" / "desc-aseg_dseg.tsv"),
            )
            _copy_any(
                dseg_tsv,
                str(
                    config.execution.output_dir / "fmriprep-rodents" / "desc-aparcaseg_dseg.tsv"
                ),
            )
        errno = 0
    finally:
        from ..patch.reports import generate_reports
        from pkg_resources import resource_filename as pkgrf

        # Generate reports phase
        failed_reports = generate_reports(
            config.execution.participant_label,
            config.execution.output_dir,
            config.execution.run_uuid,
            config=pkgrf("fmriprep_rodents", "data/reports-spec.yml"),
            packagename="fmriprep_rodents",
        )
        write_derivative_description(
            config.execution.bids_dir, config.execution.output_dir / "fmriprep-rodents"
        )

        if failed_reports and not config.execution.notrack:
            sentry_sdk.capture_message(
                "Report generation failed for %d subjects" % failed_reports,
                level="error",
            )
        sys.exit(int((errno + failed_reports) > 0))


if __name__ == "__main__":
    raise RuntimeError(
        "fmriprep_rodents/cli/run.py should not be run directly;\n"
        "Please `pip install` fmriprep-rodents and use the `fmriprep_rodents` command"
    )
