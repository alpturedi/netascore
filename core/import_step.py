import os
import re
import subprocess
from urllib.error import HTTPError
import urllib.request
import zipfile
from osgeo import ogr
from typing import List

import toolbox.helper as h
from core.db_step import DbStep
from settings import DbSettings, InputType
from toolbox.dbhelper import PostgresConnection


def create_csv(file_txt: str) -> None:
    """Takes in a path to an ogd gip txt file and converts it to a csv file."""
    with open(file_txt, 'r', encoding='iso-8859-1') as f:
        for line in f:
            if line.startswith('tbl;'):
                file_csv = open(os.path.splitext(file_txt)[0] + '.csv', 'w', encoding='utf-8')
            elif line.startswith('atr;'):
                file_csv.write(line[4:])
            elif line.startswith('rec;'):
                file_csv.write(line[4:].replace('""', '').replace('" "', ''))

    file_csv.close()


def create_sql(file_txt: str) -> None:
    """Takes in a path to an ogd gip txt file and creates a sql file from it."""
    with open(file_txt, 'r', encoding='iso-8859-1') as f:
        for line in f:
            if line.startswith('tbl;'):
                file_sql = open(os.path.splitext(file_txt)[0] + '.sql', 'w', encoding='utf-8')
                tbl = line[4:].strip().lower()
            elif line.startswith('atr;'):
                atr = line[4:].strip().lower().split(';')
            elif line.startswith('frm;'):
                frm = line[4:].strip().lower().split(';')
            elif line.startswith('rec;'):
                break

    for i, atr_ in enumerate(atr):
        if atr_ == 'offset':
            atr[i] = 'offset_'

    for i, frm_ in enumerate(frm):
        if frm_ == 'string':
            frm[i] = 'varchar'
        if m := re.search(r"^(string)[(]([0-9]*)[)]", frm_):
            length = m.group(2)
            frm[i] = f"varchar({length})"
        elif m := re.search(r"^(decimal)[(]([0-9]*)[,]([0-9]*)[)]", frm_):
            precision = m.group(2)
            scale = m.group(3)
            frm[i] = f"numeric({precision},{scale})"
        elif m := re.search(r"^(decimal)[(]([0-9]*)[)]", frm_):
            precision = m.group(2)
            if int(precision) <= 4:
                frm[i] = "smallint"
            elif int(precision) <= 10:
                frm[i] = "integer"
            elif int(precision) <= 18:
                frm[i] = "bigint"
            else:
                frm[i] = f"numeric({precision})"

    columns = [f"{atr_} {frm_}" for atr_, frm_ in zip(atr, frm)]
    sql = f"CREATE TABLE gip_{tbl} ({', '.join(columns)});"

    file_sql.write(sql)
    file_sql.close()


def import_csv(connection_string: str, path: str, schema: str, table: str) -> None:
    """Takes in a path to a csv file and imports it to a database table."""
    subprocess.run(['psql', connection_string, '-c', f"\copy {schema}.{table} from '{path}' WITH CSV DELIMITER ';' NULL '' HEADER ENCODING 'utf-8'"],
        check=True)


def import_geopackage(connection_string: str, path: str, schema: str, table: str, fid: str = None, srid: int = None, layers: List[str] = None,  attributes: List[str] = None, geometry_types: List[str] = None) -> None:  # TODO: @CW: add error handling
    """Takes in a path to a geopackage file and imports it to a database table."""
    data_source = ogr.Open(path)

    attributes = [] if attributes is None else attributes
    attributes = ','.join(attribute for attribute in attributes)

    geometry_types = [] if geometry_types is None else geometry_types
    geometry_types = ', '.join(f"'{geometry_type}'" for geometry_type in geometry_types)

    layers = [layer.GetName() for layer in data_source] if layers is None else layers
    layers_geometry_types = set(data_source.GetLayerByName(layer).GetGeomType() for layer in layers)

    fid = f"-lco FID={fid}" if fid else "-lco FID=fid"
    transform = f"-t_srs EPSG:{srid}" if srid else ""
    geometry_type = "-nlt GEOMETRY" if len(layers_geometry_types) > 1 else ""

    for layer in layers:
        h.log(f"import layer \"{layer}\"")
        geometry_column = data_source.GetLayerByName(layer).GetGeometryColumn()

        select = f"-select \"{attributes}\"" if attributes else ""
        where = f"-where \"GeometryType({geometry_column}) IN ({geometry_types})\"" if geometry_types else ""

        subprocess.run(f"ogr2ogr -f PostgreSQL \"PG:{connection_string}\" {fid} -skipfailures -lco GEOMETRY_NAME=geom -nln {schema}.{table} {transform} {geometry_type} {select} {where} \"{path}\" \"{layer}\"", 
            shell=True, check=True)


def import_osm(connection_string: str, path: str, path_style: str, schema: str, prefix: str = None) -> None:
    """Takes in a path to an osm pbf file and imports it to database tables."""
    prefix = f"--prefix {prefix}" if prefix else ""

    subprocess.run(f"osm2pgsql --database={connection_string} --middle-schema={schema} --output-pgsql-schema={schema} {prefix} --latlong --slim --hstore --style=\"{path_style}\" \"{path}\"", 
        shell=True, check=True)


class GipImporter(DbStep):
    def __init__(self, db_settings: DbSettings):
        super().__init__(db_settings)

    def run_step(self, settings: dict):
        h.info('importing gip')
        h.log(f"using import settings: {str(settings)}")

        schema = self.db_settings.entities.data_schema
        directory = self.global_settings.data_directory

        files_A = [
            {'filename': 'BikeHike.txt', 'table': 'gip_bikehike', 'columns': ['use_id']},
            {'filename': 'Link.txt', 'table': 'gip_link', 'columns': ['link_id']},
            {'filename': 'LinkCoordinate.txt', 'table': 'gip_linkcoordinate', 'columns': ['link_id', 'count']},
            {'filename': 'LinkUse.txt', 'table': 'gip_linkuse', 'columns': ['use_id']},
            {'filename': 'Link2ReferenceObject.txt', 'table': 'gip_link2referenceobject', 'columns': ['idseq']},
            {'filename': 'Node.txt', 'table': 'gip_node', 'columns': ['node_id']},
            {'filename': 'ReferenceObject.txt', 'table': 'gip_referenceobject', 'columns': ['refobj_id']},
        ]

        # open database connection
        h.log('connecting to database...')
        db = PostgresConnection.from_settings_object(self.db_settings)
        db.connect()
        db.init_extensions_and_schema(schema)

        # extract zip files
        h.logBeginTask('extract zip files')
        with zipfile.ZipFile(os.path.join(directory, settings['filename_A']), 'r') as zf:
            for file in files_A:
                if not os.path.isfile(os.path.join(directory, os.path.splitext(settings['filename_A'])[0], file['filename'])):
                    zf.extract(file['filename'], os.path.join(directory, os.path.splitext(settings['filename_A'])[0]))
        h.logEndTask()

        # create tables from files_A
        for file in files_A:
            h.logBeginTask(f"create table \"{file['table']}\"")
            if not os.path.isfile(os.path.join(directory, os.path.splitext(settings['filename_A'])[0], f"{os.path.splitext(file['filename'])[0]}.csv")):
                create_csv(os.path.join(directory, os.path.splitext(settings['filename_A'])[0], file['filename']))
            if not os.path.isfile(os.path.join(directory, os.path.splitext(settings['filename_A'])[0], f"{os.path.splitext(file['filename'])[0]}.sql")):
                create_sql(os.path.join(directory, os.path.splitext(settings['filename_A'])[0], file['filename']))

            db.drop_table(file['table'], schema=schema)
            db.execute_sql_from_file(f"{os.path.splitext(file['filename'])[0]}", os.path.join(directory, os.path.splitext(settings['filename_A'])[0]))
            db.commit()

            import_csv(db.connection_string, os.path.join(directory, os.path.splitext(settings['filename_A'])[0], f"{os.path.splitext(file['filename'])[0]}.csv"), schema, table=file['table'])

            db.add_primary_key(file['table'], file['columns'], schema=schema)
            db.commit()
            h.logEndTask()

        # close database connection
        h.log('closing database connection')
        db.close()


class OsmImporter(DbStep):
    def __init__(self, db_settings: DbSettings):
        super().__init__(db_settings)

    def _load_osm_from_bbox(self, bbox: str):
        q_template: str = """
            [timeout:900][maxsize:1073741824];
            nwr(__bbox__);
            out;"""
        q_str = q_template.replace("__bbox__", bbox)
        h.debugLog(f"prepared OSM overpass API query: \n'{q_str}")

        h.logBeginTask("Starting OSM data download...")
        curEndpointIndex = 0
        success = False
        while curEndpointIndex < len(self.global_settings.overpass_api_endpoints) and not success:
            success = False
            try:
                file_name, headers = urllib.request.urlretrieve(
                    self.global_settings.overpass_api_endpoints[curEndpointIndex] + "?data=" + urllib.parse.quote_plus(q_str), 
                    os.path.join(self.global_settings.data_directory, self.global_settings.osm_download_fname))
            except HTTPError as e:
                h.log(f"HTTPError while trying to download OSM data from {self.global_settings.overpass_api_endpoints[curEndpointIndex]}: Error code {e.code}\n{e.args}\n{e.info()} --> trying again with next available API endpoint...")
                curEndpointIndex+=1
            except BaseException as e:
                h.log(f"An unexpected ERROR occured during OSM data download from {self.global_settings.overpass_api_endpoints[curEndpointIndex]}: {e.args}")
                curEndpointIndex+=1
            else:
                success = True
                h.log(f"Response headers from API call to {self.global_settings.overpass_api_endpoints[curEndpointIndex]}: {headers}", h.LOG_LEVEL_4_DEBUG)
                h.log(f"OSM Download from {self.global_settings.overpass_api_endpoints[curEndpointIndex]} succeeded.")
        if not success:
            raise Exception("OSM data download was not successful. Terminating.")
        h.logEndTask()

    def run_step(self, settings: dict):
        h.info('importing osm')
        h.log(f"using settings: {str(settings)}")
        use_overpass_api: bool = False

        schema = self.db_settings.entities.data_schema
        directory = self.global_settings.data_directory

        # open database connection
        h.info('open database connection')
        db = PostgresConnection.from_settings_object(self.db_settings)
        db.init_extensions_and_schema(schema)

        # if needed, download OSM data
        if not h.has_keys(settings, ['filename']):
            h.info("no OSM file provided. Checking for Overpass API settings instead...")
            if not h.has_any_key(settings, ['place_name', 'bbox']):
                raise Exception("neither 'aoi_name' nor 'bbox' parameter specified for OSM download. Terminating.")
            use_overpass_api = True
            # start OSM import through overpass API
            # import from bounding box
            if h.has_keys(settings, ['bbox']):
                self._load_osm_from_bbox(settings['bbox'])
            # import from place name
            if h.has_keys(settings, ['place_name']):
                raise NotImplementedError("OSM import from place name is not yet supported.")
        

        # import osm file
        h.logBeginTask('import osm file')
        db.drop_table("osm_point", schema=schema)
        db.drop_table("osm_line", schema=schema)
        db.drop_table("osm_polygon", schema=schema)
        db.drop_table("osm_nodes", schema=schema)
        db.drop_table("osm_rels", schema=schema)
        db.drop_table("osm_roads", schema=schema)
        db.drop_table("osm_ways", schema=schema)
        db.commit()

        filename = self.global_settings.osm_download_fname
        if not use_overpass_api:
            filename = settings['filename']
        import_osm(db.connection_string, os.path.join(directory, filename), os.path.join('resources', 'default.style'), schema, prefix='osm')  # 12 m 35 s

        db.drop_table("osm_nodes", schema=schema)
        db.drop_table("osm_rels", schema=schema)
        db.drop_table("osm_roads", schema=schema)
        db.drop_table("osm_ways", schema=schema)
        db.commit()
        h.logEndTask()

        # create dataset "building"
        h.logBeginTask('create dataset "building"')
        if db.handle_conflicting_output_tables(['building'], schema):
            db.execute('''
                CREATE TABLE building AS ( -- 16 s
                    SELECT ST_Transform(way, %(target_srid)s)::geometry(Polygon, %(target_srid)s) AS geom
                    FROM osm_polygon
                    WHERE building IS NOT NULL
                );
    
                CREATE INDEX building_geom_idx ON building USING gist (geom); -- 22 s
            ''', {'target_srid':self.global_settings.target_srid})
            db.commit()
        h.logEndTask()

        # create dataset "crossing"
        h.logBeginTask('create dataset "crossing"')
        if db.handle_conflicting_output_tables(['crossing'], schema):
            db.execute('''
                CREATE TABLE crossing AS ( -- 4 s
                    SELECT ST_Transform(way, %(target_srid)s)::geometry(Point, %(target_srid)s) AS geom FROM osm_point WHERE highway IN ('crossing') UNION ALL
                    SELECT ST_Transform(way, %(target_srid)s)::geometry(LineString, %(target_srid)s) AS geom FROM osm_line WHERE highway IN ('crossing') UNION ALL
                    SELECT ST_Transform(way, %(target_srid)s)::geometry(Polygon, %(target_srid)s) AS geom FROM osm_polygon WHERE highway IN ('crossing')
                );
    
                CREATE INDEX crossing_geom_idx ON crossing USING gist (geom); -- 1 s
            ''', {'target_srid':self.global_settings.target_srid})
            db.commit()
        h.logEndTask()

        # create dataset "facility"
        h.logBeginTask('create dataset "facility"')
        if db.handle_conflicting_output_tables(['facility'], schema):
            db.execute('''
                CREATE TABLE facility AS ( -- 3 s
                    SELECT ST_Transform(way, %(target_srid)s)::geometry(Point, %(target_srid)s) AS geom
                    FROM osm_point
                    WHERE amenity IN ('arts_centre', 'artwork', 'attraction', 'bar', 'biergarten', 'cafe', 'castle', 'cinema', 'museum',
                                      'park', 'pub', 'restaurant', 'swimming_pool', 'theatre', 'viewpoint') -- entertainment
                       OR amenity IN ('bakery', 'beverages', 'butcher', 'clothes', 'department_store', 'fast_food',
                                      'florist', 'furniture_shop', 'kiosk', 'mall', 'outdoor_shop', 'pharmacy',
                                      'shoe_shop', 'sports_shop', 'supermarket', 'commercial', 'retail', 'shop') -- retail
                       OR amenity IN ('university', 'school', 'college', 'gymnasium', 'kindergarten', 'boarding_school', 'music_school',
                                      'riding_school', 'school;dormitory') -- institutional
                       OR tourism IN ('museum', 'attraction', 'gallery', 'viewpoint', 'zoo')
    
                    UNION ALL
    
                    SELECT ST_Transform(way, %(target_srid)s)::geometry(Polygon, %(target_srid)s) AS geom
                    FROM osm_polygon
                    WHERE amenity IN ('arts_centre', 'artwork', 'attraction', 'bar', 'biergarten', 'cafe', 'castle', 'cinema', 'museum',
                                      'park', 'pub', 'restaurant', 'swimming_pool', 'theatre', 'viewpoint') -- entertainment
                       OR amenity IN ('bakery', 'beverages', 'butcher', 'clothes', 'department_store', 'fast_food', 'florist',
                                      'furniture_shop', 'kiosk', 'mall', 'outdoor_shop', 'pharmacy', 'shoe_shop', 'sports_shop',
                                      'supermarket', 'commercial', 'retail', 'shop') -- retail
                       OR amenity IN ('university', 'school', 'college', 'gymnasium', 'kindergarten', 'boarding_school', 'music_school',
                                      'riding_school', 'school;dormitory') -- institutional
                       OR tourism IN ('museum', 'attraction', 'gallery', 'viewpoint', 'zoo')
                );
    
                CREATE INDEX facility_geom_idx ON facility USING gist (geom); -- 1 s
            ''', {'target_srid':self.global_settings.target_srid})
            db.commit()
        h.logEndTask()

        # create dataset "greenness"
        h.logBeginTask('create dataset "greenness"')
        if db.handle_conflicting_output_tables(['greenness'], schema):
            db.execute('''
                CREATE TABLE greenness AS ( -- 14 s
                    SELECT ST_Transform(way, %(target_srid)s)::geometry(Polygon, %(target_srid)s) AS geom
                    FROM osm_polygon
                    WHERE landuse IN ('forest', 'grass', 'meadow', 'village_green', 'recreation_ground', 'vineyard', 'flowerbed', 'farmland', 'heath', 'nature_reseve', 'park', 'greenfield')
                       OR leisure IN ('garden', 'golf_course', 'park')
                       OR "natural" IN ('tree', 'wood', 'grassland', 'heath', 'scrub')
                );
    
                CREATE INDEX greenness_geom_idx ON greenness USING gist (geom); -- 4 s
            ''', {'target_srid':self.global_settings.target_srid})
            db.commit()
        h.logEndTask()

        # create dataset "water"
        h.logBeginTask('create dataset "water"')
        if db.handle_conflicting_output_tables(['water'], schema):
            db.execute('''
                CREATE TABLE water AS ( -- 10 s
                    SELECT ST_Transform(way, %(target_srid)s)::geometry(LineString, %(target_srid)s) AS geom FROM osm_line WHERE (waterway IS NOT NULL OR "natural" = 'water') AND tunnel IS NULL UNION ALL
                    SELECT ST_Transform(way, %(target_srid)s)::geometry(Polygon, %(target_srid)s) AS geom FROM osm_polygon WHERE (waterway IS NOT NULL OR "natural" = 'water') AND tunnel IS NULL
                );
    
                CREATE INDEX water_geom_idx ON water USING gist (geom); -- 1 s
            ''', {'target_srid':self.global_settings.target_srid})
            db.commit()
        h.logEndTask()

        # close database connection
        h.log('close database connection')
        db.close()


def create_importer(db_settings: DbSettings, import_type: str):
    if import_type.lower() == InputType.GIP.value.lower():
        return GipImporter(db_settings)
    if import_type.lower() == InputType.OSM.value.lower():
        return OsmImporter(db_settings)
    raise NotImplementedError(f"import type '{import_type}' not implemented")
