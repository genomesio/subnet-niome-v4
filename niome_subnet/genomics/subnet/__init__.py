from niome_subnet.genomics.subnet.build_reference import build_reference
from niome_subnet.genomics.subnet.export_miner_bundles import export_miner_bundles
from niome_subnet.genomics.subnet.generate_observations import generate_observations

def generate_miner_bundles(miner_uids, s3_client):
    generate_observations()
    export_miner_bundles(miner_uids, s3_client)
