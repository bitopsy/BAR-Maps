import subprocess
import re
import os
import sys

def run_stats(map_dir):
    sd7_file = None
    for f in os.listdir(map_dir):
        if f.endswith('.sd7'):
            sd7_file = os.path.join(map_dir, f)
            break
    if not sd7_file:
        print(f"No .sd7 file found in {map_dir}")
        return None
    cmd = [sys.executable, 'bar_map_stats.py', sd7_file]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error running stats on {sd7_file}: {result.stderr}")
        return None
    return result.stdout

def parse_stats(output):
    data = {}
    # Extract map name and version
    match = re.search(r'={2,}\s+(.+?)\s+v([\d\.]+)\s+={2,}', output)
    if match:
        data['name'] = match.group(1)
        data['version'] = match.group(2)
    
    # Description
    match = re.search(r'Description\s+:\s+(.+)', output)
    if match:
        data['description'] = match.group(1).strip()
    
    # Archive size
    match = re.search(r'Archive\s+:\s+([^\s]+)', output)
    if match:
        data['archive_size'] = match.group(1)
    
    # Map size
    match = re.search(r'Size\s+:\s+(.+)', output)
    if match:
        data['size'] = match.group(1).strip()
    
    # Height range and water coverage
    match = re.search(r'Height range\s+:\s+(.+?)\s+water coverage\s+~([\d\.]+)%', output)
    if match:
        data['height_range'] = match.group(1).strip()
        data['water_coverage'] = float(match.group(2))
    
    # Start positions
    match = re.search(r'Start pos\s+:\s+(\d+)', output)
    if match:
        data['start_pos'] = int(match.group(1))
    
    # Gravity (optional)
    # Metal section
    match = re.search(r'Mexes\s+:\s+(\d+)', output)
    if match:
        data['mexes_total'] = int(match.group(1))
    
    match = re.search(r'Total metal/s\s+:\s+([\d\.]+)', output)
    if match:
        data['total_metal_per_sec'] = float(match.group(1))
    
    match = re.search(r'Avg m/s / mex\s+:\s+([\d\.]+)', output)
    if match:
        data['avg_metal_per_mex'] = float(match.group(1))
    
    match = re.search(r'Mex / start\s+:\s+([\d\.]+)', output)
    if match:
        data['mexes_per_start'] = float(match.group(1))
    
    # Mex distribution (spot values)
    match = re.search(r'Spot values\s+:\s+(.+)', output)
    if match:
        data['mex_spot_values'] = match.group(1).strip()
    
    # Geo vents
    match = re.search(r'Geo vents\s+:\s+(\d+)', output)
    if match:
        data['geo_vents'] = int(match.group(1))
    
    # Wind
    match = re.search(r'Wind\s+:\s+([\d\.]+) \.\. ([\d\.]+)', output)
    if match:
        data['wind_min'] = float(match.group(1))
        data['wind_max'] = float(match.group(2))
    
    # Reclaim (optional)
    # We might not need this for the README
    
    return data

def update_map_readme(map_dir, stats_data):
    readme_path = os.path.join(map_dir, 'README.md')
    if not os.path.exists(readme_path):
        print(f"README.md not found in {map_dir}")
        return
    
    with open(readme_path, 'r') as f:
        lines = f.readlines()
    
    # Find the sections to update
    in_description = False
    in_size = False
    in_terrain = False
    in_features = False
    in_mexes = False
    in_geos = False
    
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('## Map Properties'):
            # We'll replace the next few lines until the next section
            new_lines.append(line)
            # Read until we hit another section starting with '##'
            # We'll replace the whole block
            # Instead, let's rebuild the section
            # We'll skip the existing lines until next section and insert our own
            # For simplicity, we'll just replace specific lines
            pass
        elif stripped.startswith('- Description:'):
            new_lines.append(f'- Description: {stats_data.get("description", "TBD")}\n')
        elif stripped.startswith('- Size:'):
            # Format: "16 x 16 map units (8,192 x 8,192 elmos)"
            size_str = stats_data.get('size', 'TBD')
            new_lines.append(f'- Size: {size_str}\n')
        elif stripped.startswith('- Terrain Type:'):
            # Infer from water coverage or description
            water = stats_data.get('water_coverage', 0)
            if water > 40:
                terrain = "Oceanic"
            elif water > 20:
                terrain = "Mixed (Land and Water)"
            else:
                terrain = "Primarily Land"
            new_lines.append(f'- Terrain Type: {terrain}\n')
        elif stripped.startswith('- Notable Features:'):
            # Use description or maybe specific features from stats
            notable = stats_data.get('description', 'See description')
            new_lines.append(f'- Notable Features: {notable}\n')
        elif stripped.startswith('## Mexes (Metal Extractors)'):
            new_lines.append(line)
        elif stripped.startswith('- Total Mexes:'):
            new_lines.append(f'- Total Mexes: {stats_data.get("mexes_total", "TBD")}\n')
        elif stripped.startswith('- Starting Mexes per team:'):
            per_team = stats_data.get('mexes_per_start', 'TBD')
            new_lines.append(f'- Starting Mexes per team: {per_team:.2f}\n')
        elif stripped.startswith('- Mex Distribution:'):
            dist = stats_data.get('mex_spot_values', 'TBD')
            new_lines.append(f'- Mex Distribution: {dist}\n')
        elif stripped.startswith('## Geothermal Vents (Geos)'):
            new_lines.append(line)
        elif stripped.startswith('- Total Geos:'):
            new_lines.append(f'- Total Geos: {stats_data.get("geo_vents", "TBD")}\n')
        elif stripped.startswith('## Strategic Notes'):
            new_lines.append(line)
        else:
            new_lines.append(line)
    
    with open(readme_path, 'w') as f:
        f.writelines(new_lines)

def main():
    base_dir = os.getcwd()
    # List of map directories (excluding files)
    items = os.listdir(base_dir)
    map_dirs = [d for d in items if os.path.isdir(d) and not d.startswith('.')]
    
    for d in sorted(map_dirs):
        print(f"Processing {d}...")
        stats_output = run_stats(d)
        if stats_output is None:
            continue
        stats_data = parse_stats(stats_output)
        print(f"  Stats: {stats_data}")
        update_map_readme(d, stats_data)
    
    # Also update the main README.md to include map size in notes
    main_readme = os.path.join(base_dir, 'README.md')
    if os.path.exists(main_readme):
        update_main_readme(main_readme, base_dir)

def update_main_readme(readme_path, base_dir):
    with open(readme_path, 'r') as f:
        lines = f.readlines()
    
    # Find the table of maps
    in_table = False
    new_lines = []
    for line in lines:
        if line.strip().startswith('| Map |'):
            in_table = True
            new_lines.append(line)
            continue
        if in_table and line.strip() == '':
            # End of table? Actually, the table ends before the ## Directories
            # We'll stop when we hit a line that starts with '##'
            if line.startswith('##'):
                in_table = False
        if in_table and not line.startswith('|') and not line.startswith('-'):
            # Still in table but not a row? Actually, the separator line is |-----
            # We'll keep it.
            pass
        
        if in_table and line.startswith('| ') and '|' in line:
            # This is a row in the table
            parts = line.split('|')
            if len(parts) >= 6:
                map_name = parts[1].strip()
                # Find the directory for this map name (might need mapping)
                # For simplicity, we'll assume the directory name matches the map name in the table
                # But the table has some entries like "AKUMA" and directory "AKUMA"
                # Some have versions: "avatar_v2" vs directory "AVATAR"
                # We'll do a lookup: check if there's a directory that matches ignoring case and underscores/hyphens
                found = False
                for d in os.listdir(base_dir):
                    if os.path.isdir(os.path.join(base_dir, d)):
                        # Normalize both for comparison
                        norm_map = map_name.lower().replace('-', '').replace('_', '')
                        norm_dir = d.lower().replace('-', '').replace('_', '')
                        if norm_map == norm_dir:
                            # Found the directory, get its size
                            sd7_file = None
                            for f in os.listdir(os.path.join(base_dir, d)):
                                if f.endswith('.sd7'):
                                    sd7_file = os.path.join(base_dir, d, f)
                                    break
                            if sd7_file:
                                # Run stats quickly to get size
                                cmd = [sys.executable, 'bar_map_stats.py', sd7_file]
                                result = subprocess.run(cmd, capture_output=True, text=True)
                                if result.returncode == 0:
                                    # Extract size
                                    match = re.search(r'Size\s+:\s+(.+)', result.stdout)
                                    if match:
                                        size_str = match.group(1).strip()
                                        # Update the notes column (index 5)
                                        parts[5] = f' {size_str} |'
                                        line = '|'.join(parts)
                                        found = True
                                        break
                if not found:
                    # Keep original line
                    pass
            new_lines.append(line)
        else:
            new_lines.append(line)
            if line.startswith('##'):
                in_table = False
    
    with open(readme_path, 'w') as f:
        f.writelines(new_lines)

if __name__ == '__main__':
    main()