#   -*- coding: utf-8 -*-
#
#   This file is part of PyBuilder
#
#   Copyright 2011-2015 PyBuilder Team
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import imp
import multiprocessing
import sys

try:
    from StringIO import StringIO
except ImportError as e:
    from io import StringIO

from pybuilder.core import init, after, use_plugin
from pybuilder.utils import discover_modules, render_report
from pybuilder.errors import BuildFailedException

use_plugin("python.core")
use_plugin("analysis")


@init
def init_coverage_properties(project):
    project.build_depends_on("coverage")

    project.set_property_if_unset("coverage_threshold_warn", 70)
    project.set_property_if_unset("coverage_break_build", True)
    project.set_property_if_unset("coverage_reload_modules", True)
    project.set_property_if_unset("coverage_exceptions", [])
    project.set_property_if_unset("coverage_fork", False)


@after(("analyze", "verify"), only_once=True)
def verify_coverage(project, logger, reactor):
    run_coverage(project, logger, reactor, "coverage", "coverage", "run_unit_tests")


def run_coverage(project, logger, reactor, execution_prefix, execution_name, target_task):
    logger.info("Collecting coverage information")

    if project.get_property("%s_fork" % execution_prefix):
        logger.debug("Forking process to do %s analysis", execution_name)
        process = multiprocessing.Process(target=do_coverage,
                                          args=(
                                              project, logger, reactor, execution_prefix, execution_name,
                                              target_task))
        process.start()
        process.join()
        if process.exitcode and project.get_property("%s_break_build" % execution_prefix):
            raise BuildFailedException(
                "Forked %s process indicated failure with error code %d" % (execution_name, process.exitcode))
    else:
        do_coverage(project, logger, reactor, execution_prefix, execution_name,
                    target_task)


def do_coverage(project, logger, reactor, execution_prefix, execution_name, target_task):
    from coverage import coverage as coverage_factory

    source_tree_path = project.get_property("dir_source_main_python")
    coverage = coverage_factory(cover_pylib=False, source=[source_tree_path])
    _start_coverage(coverage)
    project.set_property('__running_coverage', True)  # tell other plugins that we are not really unit testing right now
    reactor.execute_task(target_task)
    project.set_property('__running_coverage', False)

    _stop_coverage(coverage, project, logger, execution_prefix)

    coverage_too_low = False
    threshold = project.get_property("%s_threshold_warn" % execution_prefix)
    exceptions = project.get_property("%s_exceptions" % execution_prefix)

    report = {
        "module_names": []
    }

    sum_lines = 0
    sum_lines_not_covered = 0

    module_names = _discover_modules_to_cover(project)
    modules = []
    for module_name in module_names:
        try:
            module = sys.modules[module_name]
        except KeyError:
            logger.warn("Module not imported: {0}. No coverage information available.".format(module_name))
            continue

        modules.append(module)

        module_report_data = build_module_report(coverage, module)
        should_ignore_module = module_name in exceptions

        if not should_ignore_module:
            sum_lines += module_report_data[0]
            sum_lines_not_covered += module_report_data[2]

        module_report = {
            "module": module_name,
            "coverage": module_report_data[4],
            "sum_lines": module_report_data[0],
            "lines": module_report_data[1],
            "sum_lines_not_covered": module_report_data[2],
            "lines_not_covered": module_report_data[3],
        }

        report["module_names"].append(module_report)

        if module_report_data[4] < threshold:
            msg = "Test coverage below %2d%% for %s: %2d%%" % (threshold, module_name, module_report_data[4])
            if not should_ignore_module:
                logger.warn(msg)
                coverage_too_low = True
            else:
                logger.info(msg)

    if sum_lines == 0:
        overall_coverage = 0
    else:
        overall_coverage = (sum_lines - sum_lines_not_covered) * 100 / sum_lines
    report["overall_coverage"] = overall_coverage

    if overall_coverage < threshold:
        logger.warn("Overall %s is below %2d%%: %2d%%", execution_name, threshold, overall_coverage)
        coverage_too_low = True
    else:
        logger.info("Overall %s is %2d%%", execution_name, overall_coverage)

    project.write_report("%s.json" % execution_prefix, render_report(report))

    _write_summary_report(coverage, project, modules, execution_prefix)

    if coverage_too_low and project.get_property("%s_break_build" % execution_prefix):
        raise BuildFailedException("Test coverage for at least one module is below %d%%", threshold)


def _start_coverage(coverage):
    coverage.erase()
    coverage.start()


def _stop_coverage(coverage, project, logger, execution_prefix):
    _reimport_source_modules(project, logger, execution_prefix)
    coverage.stop()


def _reimport_source_modules(project, logger, execution_prefix):
    if project.get_property("%s_reload_modules" % execution_prefix):
        modules = _discover_modules_to_cover(project)
        for module in modules:
            logger.debug("Reloading module %s", module)
            if module in sys.modules:
                imp.reload(sys.modules[module])


def build_module_report(coverage, module):
    analysis_result = coverage.analysis(module)

    lines_total = len(analysis_result[1])
    lines_not_covered = len(analysis_result[2])
    lines_covered = lines_total - lines_not_covered

    if lines_total == 0:
        code_coverage = 100
    elif lines_covered == 0:
        code_coverage = 0
    else:
        code_coverage = lines_covered * 100 / lines_total

    return (lines_total, analysis_result[1],
            lines_not_covered, analysis_result[2],
            code_coverage)


def _write_summary_report(coverage, project, modules, execution_prefix):
    from coverage import CoverageException

    summary = StringIO()
    coverage.report(modules, file=summary)
    try:
        coverage.xml_report(outfile=project.expand_path("$dir_reports/%s.xml" % execution_prefix))
        coverage.save()
    except CoverageException:
        pass  # coverage raises when there is no data
    project.write_report(execution_prefix, summary.getvalue())
    summary.close()


def _discover_modules_to_cover(project):
    return discover_modules(project.expand_path("$dir_source_main_python"))
