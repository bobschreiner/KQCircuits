# This code is part of KQCircuits
# Copyright (C) 2022 IQM Finland Oy
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see
# https://www.gnu.org/licenses/gpl-3.0.html.
#
# The software distribution should follow IQM trademark policy for open-source software
# (meetiqm.com/iqm-open-source-trademark-policy). IQM welcomes contributions to the code.
# Please see our contribution agreements for individuals (meetiqm.com/iqm-individual-contributor-license-agreement)
# and organizations (meetiqm.com/iqm-organization-contributor-license-agreement).
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import gmsh
from scipy.constants import mu_0, epsilon_0

from elmer_helpers import (
    sif_capacitance,
    sif_inductance,
    sif_circuit_definitions,
    use_london_equations,
)

from gmsh_helpers import (
    separated_hull_and_holes,
    add_polygon,
    get_recursive_children,
    set_meshing,
    apply_elmer_layer_prefix,
    get_metal_layers,
    optimize_mesh,
)

try:
    import pya
except ImportError:
    import klayout.db as pya

angular_frequency = 5e2  # a constant for inductance simulations.
# technically is needed but doesn't affect results
# howewer, if very high, might have an unwanted effect


def produce_cross_section_mesh(json_data: dict[str, Any], msh_file: Path | str) -> None:
    """
    Produces 2D cross-section mesh and optionally runs the Gmsh GUI

    Args:
        json_data: all the model data produced by `export_elmer_json`
        msh_file: mesh file name
    """

    if Path(msh_file).exists():
        logging.info(f"Reusing existing mesh from {str(msh_file)}")
        return

    # Initialize gmsh
    gmsh.initialize()

    # Read geometry from gds file
    layout = pya.Layout()
    layout.read(json_data["gds_file"])
    cell = layout.top_cell()
    bbox = cell.bbox().to_dtype(layout.dbu)
    layers = json_data["layers"]

    # Create mesh using geometries in gds file
    gmsh.model.add("cross_section")

    dim_tags = {}
    for name, data in layers.items():
        reg = pya.Region(cell.shapes(layout.layer(data["layer"], 0)))
        layer_dim_tags = []
        for simple_poly in reg.each():
            poly = separated_hull_and_holes(simple_poly)
            hull_point_coordinates = [
                (point.x * layout.dbu, point.y * layout.dbu, 0) for point in poly.each_point_hull()
            ]
            hull_plane_surface_id, _ = add_polygon(hull_point_coordinates)
            hull_dim_tag = (2, hull_plane_surface_id)
            hole_dim_tags = []
            for hole in range(poly.holes()):
                hole_point_coordinates = [
                    (point.x * layout.dbu, point.y * layout.dbu, 0) for point in poly.each_point_hole(hole)
                ]
                hole_plane_surface_id, _ = add_polygon(hole_point_coordinates)
                hole_dim_tags.append((2, hole_plane_surface_id))
            if hole_dim_tags:
                layer_dim_tags += gmsh.model.occ.cut([hull_dim_tag], hole_dim_tags)[0]
            else:
                layer_dim_tags.append(hull_dim_tag)
        dim_tags[name] = layer_dim_tags

    # Call fragment and get updated dim_tags as new_tags. Then synchronize.
    all_dim_tags = [tag for tags in dim_tags.values() for tag in tags]
    _, dim_tags_map_imp = gmsh.model.occ.fragment(all_dim_tags, [], removeTool=False)
    dim_tags_map = dict(zip(all_dim_tags, dim_tags_map_imp))
    new_tags = {
        name: [new_tag for old_tag in tags for new_tag in dim_tags_map[old_tag]] for name, tags in dim_tags.items()
    }
    gmsh.model.occ.synchronize()

    # Set meshing
    mesh_size = json_data.get("mesh_size", {})
    workflow = json_data.get("workflow", {})
    set_meshing(mesh_size, new_tags, workflow)

    # Add excitation boundaries
    metal_layers = get_metal_layers(layers)
    excitations = {d["excitation"] for d in metal_layers.values()}
    for excitation in excitations:
        exc_dts = [dt for n, d in metal_layers.items() if d["excitation"] == excitation for dt in new_tags.get(n, [])]
        new_tags[f"excitation_{excitation}_boundary"] = [(d, t) for d, t in get_recursive_children(exc_dts) if d == 1]

    # Include outer boundaries to new_tags
    new_tags.update(get_outer_bcs(bbox))

    # Create physical groups from each object in new_tags
    for name, dts in new_tags.items():
        if dts:
            gmsh.model.addPhysicalGroup(
                max(d for d, _ in dts), [t for _, t in dts], name=apply_elmer_layer_prefix(name)
            )

    # Generate and save mesh
    gmsh.model.mesh.generate(2)

    optimize_mesh(json_data.get("mesh_optimizer"))
    gmsh.write(str(msh_file))

    # Open mesh viewer
    if workflow.get("run_gmsh_gui", False):
        gmsh.fltk.run()

    gmsh.finalize()


def get_outer_bcs(bbox: pya.DBox, beps: float = 1e-6) -> dict[str, list[tuple[int, int]]]:
    """
    Returns the outer boundary dim tags for `xmin`, `xmax`, `ymin` and `ymax`.

    Args:
        bbox: bounding box in klayout format
        beps: tolerance for the search bounding box

    Returns:
        dictionary with outer boundary dim tags
    """
    outer_bc_dim_tags = {}
    outer_bc_dim_tags["xmin_boundary"] = gmsh.model.occ.getEntitiesInBoundingBox(
        bbox.p1.x - beps, bbox.p1.y - beps, -beps, bbox.p1.x + beps, bbox.p2.y + beps, beps, dim=1
    )
    outer_bc_dim_tags["xmax_boundary"] = gmsh.model.occ.getEntitiesInBoundingBox(
        bbox.p2.x - beps, bbox.p1.y - beps, -beps, bbox.p2.x + beps, bbox.p2.y + beps, beps, dim=1
    )
    outer_bc_dim_tags["ymin_boundary"] = gmsh.model.occ.getEntitiesInBoundingBox(
        bbox.p1.x - beps, bbox.p1.y - beps, -beps, bbox.p2.x + beps, bbox.p1.y + beps, beps, dim=1
    )
    outer_bc_dim_tags["ymax_boundary"] = gmsh.model.occ.getEntitiesInBoundingBox(
        bbox.p1.x - beps, bbox.p2.y - beps, -beps, bbox.p2.x + beps, bbox.p2.y + beps, beps, dim=1
    )
    return outer_bc_dim_tags


def produce_cross_section_sif_files(json_data: dict[str, Any], folder_path: Path) -> list[str]:
    """
    Produces sif files required for capacitance and inductance simulations.

    Args:
        json_data: all the model data produced by `export_elmer_json`
        folder_path: folder path for the sif files

    Returns:
        sif file paths
    """

    def save(file_name: str, content: str) -> str:
        """Saves file with content given in string format. Returns name of the saved file."""
        with open(Path(folder_path).joinpath(file_name), "w", encoding="utf-8") as f:
            f.write(content)
        return file_name

    folder_path.mkdir(exist_ok=True, parents=True)
    sif_names = json_data["sif_names"]

    sif_files = [
        save(
            f"{sif_names[0]}.sif",
            sif_capacitance(json_data, folder_path, vtu_name=sif_names[0], angular_frequency=0, dim=2, with_zero=False),
        )
    ]
    if json_data["run_inductance_sim"]:
        if use_london_equations(json_data):
            circuit_definitions_file = save("inductance.definitions", sif_circuit_definitions(json_data))
            sif_files.append(
                save(
                    f"{sif_names[1]}.sif",
                    sif_inductance(json_data, folder_path, angular_frequency, circuit_definitions_file),
                )
            )
        else:
            sif_files.append(
                save(
                    f"{sif_names[1]}.sif",
                    sif_capacitance(
                        json_data, folder_path, vtu_name=sif_names[1], angular_frequency=0, dim=2, with_zero=True
                    ),
                )
            )
    return sif_files


def get_cross_section_capacitance_and_inductance(
    json_data: dict[str, Any], folder_path: Path
) -> dict[str, list[list[float]] | None]:
    """
    Returns capacitance and inductance matrices stored in simulation output files.

    Args:
        json_data: all the model data produced by `export_elmer_json`
        folder_path: folder path for the sif files

    Returns:
        Cs and Ls matrices
    """
    try:
        c_matrix_file = Path(folder_path).joinpath("capacitance.dat")
        c_matrix = pd.read_csv(c_matrix_file, sep=r"\s+", header=None).values
    except FileNotFoundError:
        return {"Cs": None, "Ls": None}

    try:
        if use_london_equations(json_data):
            l_matrix_file_name = "inductance.dat"
            l_matrix_file = Path(folder_path).joinpath(l_matrix_file_name)
            if not l_matrix_file.is_file():
                l_matrix_file = Path(folder_path).joinpath(f"{l_matrix_file_name}.0")
            data = pd.read_csv(l_matrix_file, sep=r"\s+", header=None)
            l_matrix_file = Path(folder_path).joinpath(l_matrix_file_name)
            with open(f"{l_matrix_file}.names", encoding="utf-8") as names:
                data.columns = [
                    line.split("res: ")[1].replace("\n", "") for line in names.readlines() if "res:" in line
                ]
            voltage = data["v_component(1) re"] + 1.0j * data["v_component(1) im"]
            current = data["i_component(1) re"] + 1.0j * data["i_component(1) im"]
            impedance = voltage / current
            l_matrix = np.array([np.imag(impedance) / angular_frequency])
        else:
            c0_matrix_file = Path(folder_path).joinpath("capacitance0.dat")
            c0_matrix = pd.read_csv(c0_matrix_file, sep=r"\s+", header=None).values
            l_matrix = mu_0 * epsilon_0 * np.linalg.inv(c0_matrix)
    except FileNotFoundError:
        return {"Cs": c_matrix.tolist(), "Ls": None}

    return {"Cs": c_matrix.tolist(), "Ls": l_matrix.tolist()}
