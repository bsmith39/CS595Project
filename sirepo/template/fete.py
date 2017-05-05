# -*- coding: utf-8 -*-
u"""FETE/WARP execution template.

:copyright: Copyright (c) 2017 RadiaSoft LLC.  All Rights Reserved.
:license: http://www.apache.org/licenses/LICENSE-2.0.html
"""

from __future__ import absolute_import, division, print_function
from pykern import pkcollections
from pykern import pkio
from pykern import pkjinja
from pykern.pkdebug import pkdc, pkdp
from rswarp.cathode import sources
from rswarp.utilities.file_utils import readparticles
from scipy import constants
from sirepo import simulation_db
from sirepo.template import template_common
import h5py
import numpy as np
import os.path
import re

SIM_TYPE = 'fete'

#TODO(pjm): change to True
WANT_BROWSER_FRAME_CACHE = False

_PARTICLE_PERIOD = 100


def background_percent_complete(report, run_dir, is_running, schema):
    files = _h5_file_list(run_dir, 'currentAnimation')
    if len(files) < 2:
        return {
            'percentComplete': 0,
            'frameCount': 0,
        }
    file_index = len(files) - 1
    last_update_time = int(os.path.getmtime(str(files[file_index])))
    # look at 2nd to last file if running, last one may be incomplete
    if is_running:
        file_index -= 1
    data = simulation_db.read_json(run_dir.join(template_common.INPUT_BASE_NAME))
    percent_complete = (file_index + 1.0) * _PARTICLE_PERIOD / data.models.simulationGrid.num_steps

    if percent_complete < 0:
        percent_complete = 0
    elif percent_complete > 1.0:
        percent_complete = 1.0
    return {
        'lastUpdateTime': last_update_time,
        'percentComplete': percent_complete * 100,
        'frameCount': file_index + 1,
    }


def fixup_old_data(data):
    pass


def get_animation_name(data):
    return 'animation'


def get_zcurrent_new(particle_array, momenta, mesh, particle_weight, dz):
    """
    Find z-directed current on a per cell basis
    particle_array: z positions at a given step
    momenta: particle momenta at a given step in SI units
    mesh: Array of Mesh spacings
    particle_weight: Weight from Warp
    dz: Cell Size
    """
    current = np.zeros_like(mesh)
    velocity = constants.c * momenta / np.sqrt(momenta**2 + (constants.electron_mass * constants.c)**2) * particle_weight

    for index, zval in enumerate(particle_array):
        bucket = np.round(zval/dz) #value of the bucket/index in the current array
        current[int(bucket)] += velocity[index]

    return current * constants.elementary_charge / dz


def get_simulation_frame(run_dir, data, model_data):
    frame_index = int(data['frameIndex'])
    args = data['animationArgs'].split('_')
    if data['modelName'] == 'currentAnimation':
        data_file = open_data_file(run_dir, data['modelName'], frame_index)
        return _extract_current(model_data, data_file)
    if data['modelName'] == 'fieldAnimation':
        data_file = open_data_file(run_dir, data['modelName'], frame_index)
        return _extract_field(args[0], model_data, data_file)
    if data['modelName'] == 'particleAnimation':
        return _extract_particle(run_dir, args[0], model_data)
    raise RuntimeError('{}: unknown simulation frame model'.format(data['modelName']))


def lib_files(data, source_lib):
    """No lib files"""
    return template_common.internal_lib_files([], source_lib)


def models_related_to_report(data):
    """What models are required for this data['report']

    Args:
        data (dict): simulation
    Returns:
        list: Named models, model fields or values (dict, list) that affect report
    """
    return []


def new_simulation(data, new_simulation_data):
    pass


def open_data_file(run_dir, model_name, file_index=None):
    """Opens data file_index'th in run_dir

    Args:
        run_dir (py.path): has subdir ``hdf5``
        file_index (int): which file to open (default: last one)

    Returns:
        OrderedMapping: various parameters
    """
    files = _h5_file_list(run_dir, model_name)
    res = pkcollections.OrderedMapping()
    res.num_frames = len(files)
    res.frame_index = res.num_frames - 1 if file_index is None else file_index
    res.filename = str(files[res.frame_index])
    res.iteration = int(re.search(r'data(\d+)', res.filename).group(1))
    return res


def prepare_aux_files(run_dir, data):
    template_common.copy_lib_files(data, None, run_dir)


def prepare_for_client(data):
    return data


def prepare_for_save(data):
    return data


def remove_last_frame(run_dir):
    for m in ('currentAnimation', 'fieldAnimation'):
        files = _h5_file_list(run_dir, m)
        if len(files) > 0:
            pkio.unchecked_remove(files[-1])


def write_parameters(data, schema, run_dir, is_parallel):
    """Write the parameters file

    Args:
        data (dict): input
        schema (dict): to validate data
        run_dir (py.path): where to write
        is_parallel (bool): run in background?
    """
    pkio.write_text(
        run_dir.join(template_common.PARAMETERS_PYTHON_FILE),
        _generate_parameters_file(
            data,
            run_dir,
            is_parallel,
        ),
    )


def _extract_current(data, data_file):
    grid = data['models']['simulationGrid']
    plate_spacing = grid['plate_spacing'] * 1e-6
    dz = plate_spacing / grid['num_z']
    zmesh = np.linspace(0, plate_spacing, grid['num_z'] + 1) #holds the z-axis grid points in an array
    report_data = readparticles(data_file.filename)
    data_time = report_data['time']
    with h5py.File(data_file.filename, 'r') as f:
        weights = np.array(f['data/{}/particles/beam/weighting'.format(data_file.iteration)])

    curr = get_zcurrent_new(report_data['beam'][:,4], report_data['beam'][:,5], zmesh, weights, dz)
    beam = data['models']['beam']
    cathode_area = 2. * beam['x_radius'] * 1e-6
    RD_ideal = sources.j_rd(beam['cathode_temperature'], beam['cathode_work_function']) * cathode_area
    JCL_ideal = sources.cl_limit(beam['cathode_work_function'], beam['anode_work_function'], beam['grid_bias'], plate_spacing) * cathode_area

    if beam['currentMode'] == '2' or (beam['currentMode'] == '1' and beam['beam_current'] >= JCL_ideal):
        curr2 = np.full_like(zmesh, JCL_ideal)
        y2_title = 'Child-Langmuir cold limit'
    else:
        curr2 = np.full_like(zmesh, RD_ideal)
        y2_title = 'Richardson-Dushman'
    return {
        'title': 'Current for Time: {:.4e}s'.format(data_time),
        'x_range': [0, plate_spacing],
        'y_label': 'Current [A]',
        'x_label': 'Z [m]',
        'points': [
            curr.tolist(),
            curr2.tolist(),
        ],
        'x_points': zmesh.tolist(),
        'y_range': [min(np.min(curr), np.min(curr2)), max(np.max(curr), np.max(curr2))],
        'y1_title': 'Current',
        'y2_title': y2_title,
    }


def _extract_field(field, data, data_file):
    grid = data['models']['simulationGrid']
    plate_spacing = grid['plate_spacing'] * 1e-6
    beam = data['models']['beam']
    radius = beam['x_radius'] * 1e-6
    selector = field
    if not field == 'phi':
        selector = 'E/{}'.format(field)
    with h5py.File(data_file.filename, 'r') as f:
        values = np.array(f['data/{}/meshes/{}'.format(data_file.iteration, selector)])
        data_time = f['data/{}'.format(data_file.iteration)].attrs['time']
        dt = f['data/{}'.format(data_file.iteration)].attrs['dt']
    if field == 'phi':
        values = values[0,:,:]
        title = 'ϕ'
    else:
        values = values[:,0,:]
        title = 'E {}'.format(field)
    return {
        'x_range': [0, plate_spacing, len(values[0])],
        'y_range': [- radius, radius, len(values)],
        'x_label': 'z [m]',
        'y_label': 'x [m]',
        'title': '{} for Time: {:.4e}s, Step {}'.format(title, data_time, data_file.iteration),
        'z_matrix': np.flipud(values).tolist(),
    }


def _extract_particle(run_dir, render_count, data):
    v = np.load(str(run_dir.join('particles.npy')))
    kept_electrons = v[0]
    lost_electrons = v[1]
    grid = data['models']['simulationGrid']
    plate_spacing = grid['plate_spacing'] * 1e-6
    beam = data['models']['beam']
    radius = beam['x_radius'] * 1e-6
    x_points = []
    y_points = []
    for i in range(len(kept_electrons[1])):
        x_points.append(kept_electrons[1][i].tolist())
        y_points.append(kept_electrons[0][i].tolist())

    return {
        'title': 'Particle Trace',
        'x_range': [0, plate_spacing],
        'y_label': 'x [m]',
        'x_label': 'z [m]',
        'points': y_points,
        'x_points': x_points,
        'y_range': [-radius, radius],
    }


def _generate_parameters_file(data, run_dir=None, is_parallel=False):
    template_common.validate_models(data, simulation_db.get_schema(SIM_TYPE))
    v = template_common.flatten_data(data['models'], {})
    v['outputDir'] = '"{}"'.format(run_dir) if run_dir else None
    v['particlePeriod'] = _PARTICLE_PERIOD
    return pkjinja.render_resource('fete.py', v)


def _h5_file_list(run_dir, model_name):
    return pkio.walk_tree(
        run_dir.join('diags/xzsolver/hdf5' if model_name == 'currentAnimation' else 'diags/fields/electric'),
        r'\.h5$',
    )
