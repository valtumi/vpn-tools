#!/usr/bin/env python3

from datetime import datetime, UTC
import speedtest


# Monkey patch speedtest-cli's timestamp generation
def patched_timestamp(self):
    return f"{datetime.now(UTC).isoformat()}Z"


speedtest.Speedtest.timestamp = property(patched_timestamp)

import subprocess
import json
import re
import time
from typing import List, Dict, Tuple
import statistics
from dataclasses import dataclass
import logging
import sys
from geopy.distance import geodesic
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut
import argparse
from mullvad_coordinates import get_coordinates
import random

MAX_SERVERS_TO_TEST = 20
DEFAULT_LOCATION = "Lijiang, Yunnan, China"
LIJIANG_COORDS = (26.8721, 100.2299)  # Default coordinates for Lijiang
TOP_SERVERS_NUM = 50
RANDOM_SELECTION = True
MAX_CONNECTION_TIMEOUT = 60

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("mullvad_speed_test.log"),
    ],
)
logger = logging.getLogger(__name__)


@dataclass
class ServerInfo:
    country: str
    city: str
    hostname: str
    protocol: str
    provider: str
    ownership: str
    ip: str
    ipv6: str
    connection_time: float = 0  # Time in seconds to establish connection
    connection_status: str = "disconnected"
    latitude: float = 0.0
    longitude: float = 0.0
    distance_km: float = 0.0  # Distance from reference location


@dataclass
class SpeedTestResult:
    download_speed: float  # Mbps
    upload_speed: float  # Mbps
    ping: float  # ms
    jitter: float  # ms
    packet_loss: float  # percentage


@dataclass
class MtrResult:
    avg_latency: float  # ms
    packet_loss: float  # percentage
    hops: int


class MullvadTester:
    def __init__(
        self, target_host: str = "8.8.8.8", reference_location: str = DEFAULT_LOCATION
    ):
        self.target_host = target_host
        self.reference_location = reference_location
        self.reference_coords = self._get_location_coordinates(reference_location)
        self.servers = self._get_servers()
        self.results: Dict[str, Tuple[SpeedTestResult, MtrResult]] = {}
        self.max_connection_timeout = MAX_CONNECTION_TIMEOUT  # in seconds

        if not self.servers:
            logger.error(
                "No Mullvad servers found. Please check if Mullvad is installed and accessible."
            )
            sys.exit(1)

        logger.info(f"Found {len(self.servers)} Mullvad servers")
        logger.info(
            f"Reference location: {reference_location} ({self.reference_coords})"
        )

    def _get_location_coordinates(self, location: str) -> Tuple[float, float]:
        """Get coordinates for a location using geocoding."""

        try:
            geolocator = Nominatim(user_agent="mullvad_speed_test")
            location_data = geolocator.geocode(location, exactly_one=True)

            if location_data:
                coords = (location_data.latitude, location_data.longitude)
                logger.info(f"Found coordinates for {location}: {coords}")
                logger.info(f"Full location data: {location_data.address}")
                return coords
            else:
                logger.error(
                    f"Could not find coordinates for {location}, using default Lijiang coordinates"
                )
                if location.lower().startswith("lijiang"):
                    return LIJIANG_COORDS
                else:
                    logger.error(
                        "Please verify your location string or use a more specific location"
                    )
                    sys.exit(1)
        except (GeocoderTimedOut, Exception) as e:
            logger.error(f"Error getting coordinates for {location}: {e}")
            if location.lower().startswith("lijiang"):
                return LIJIANG_COORDS
            else:
                logger.error(
                    "Please verify your location string or use a more specific location"
                )
                sys.exit(1)

    def _extract_coordinates(self, line: str) -> Tuple[float, float]:
        """Extract coordinates from server location line."""
        # Log the input line for debugging
        logger.debug(f"Extracting coordinates from line: '{line}'")

        # Look for coordinates with optional negative signs
        coords_match = re.search(
            r"@\s*([-]?\d+\.?\d*)°([NS]),\s*([-]?\d+\.?\d*)°([EW])", line
        )
        if coords_match:
            # Log the matched groups
            logger.debug(f"Raw matches:")
            logger.debug(
                f"  Latitude: '{coords_match.group(1)}' '{coords_match.group(2)}'"
            )
            logger.debug(
                f"  Longitude: '{coords_match.group(3)}' '{coords_match.group(4)}'"
            )

            # Extract latitude - if it's negative, it's actually Southern hemisphere
            lat = float(coords_match.group(1))
            if lat < 0:
                # If latitude is negative, it's actually Southern hemisphere regardless of N/S marker
                lat = lat  # Keep it negative
            else:
                # Use the hemisphere marker only for positive latitudes
                lat = lat if coords_match.group(2) == "N" else -lat
            logger.debug(f"Processed latitude: {lat}")

            # Extract longitude
            lon = float(coords_match.group(3))
            # For Australian cities (which have negative latitude), assume East longitude
            if lat < 0 and "au-" in line.lower():
                lon = abs(lon)  # Force positive for Australian cities
            else:
                # Normal handling for other locations
                lon = lon if coords_match.group(4) == "E" else -lon

            logger.debug(f"Final longitude: {lon}")
            logger.debug(f"Final coordinates: ({lat}, {lon})")
            return (lat, lon)

        logger.warning(f"Failed to match coordinates in line: '{line}'")
        return (0.0, 0.0)

    def _calculate_distance(self, server_coords: Tuple[float, float]) -> float:
        """Calculate distance between server and reference location."""
        if server_coords == (0.0, 0.0) or self.reference_coords == (0.0, 0.0):
            return float("inf")

        distance = geodesic(self.reference_coords, server_coords).kilometers
        logger.debug(f"Distance calculation:")
        logger.debug(f"  Reference: {self.reference_coords}")
        logger.debug(f"  Server: {server_coords}")
        logger.debug(f"  Distance: {distance:.2f} km")
        return distance

    def _get_servers(self) -> List[ServerInfo]:
        """Parse mullvad relay list output to get server information."""
        servers = []
        try:
            logger.info("Fetching Mullvad server list...")
            output = subprocess.check_output(["mullvad", "relay", "list"], text=True)

            logger.debug("Got server list output")

            current_country = ""
            current_city = ""

            for line in output.split("\n"):
                line = line.strip()
                if not line:
                    continue

                logger.debug(f"Processing line: {line}")

                # Parse country
                country_match = re.match(r"^([A-Za-z\s]+)\s+\(([a-z]{2})\)$", line)
                if country_match:
                    current_country = country_match.group(1)
                    logger.debug(f"Found country: {current_country}")
                    continue

                # Parse city
                city_match = re.match(
                    r"^\s*([A-Za-z\s,]+)\s+\([a-z]+\)\s+@\s+[-\d.]+°[NS],\s+[-\d.]+°[EW]$",
                    line,
                )
                if city_match:
                    current_city = city_match.group(1)
                    logger.debug(f"Found city: {current_city}")
                    # Get coordinates from our database instead of parsing from Mullvad output
                    current_coords = get_coordinates(current_city, current_country)
                    logger.debug(f"Using coordinates from database: {current_coords}")
                    continue

                # Parse server
                server_match = re.match(
                    r"^\s*([a-z]{2}-[a-z]+-(wg|ovpn)-\d+)\s+\(([^,]+)(?:,\s*([^)]+))?\)\s+-\s+([^,]+)(?:,\s+hosted by ([^()]+))?\s+\(([^)]+)\)$",
                    line,
                )
                if server_match:
                    hostname = server_match.group(1)
                    protocol = server_match.group(2)
                    ip = server_match.group(3)
                    ipv6 = server_match.group(4) if server_match.group(4) else ""
                    provider = server_match.group(5) if server_match.group(5) else ""
                    ownership = server_match.group(6)

                    # Calculate distance from reference location
                    distance = self._calculate_distance(current_coords)

                    logger.debug(
                        f"Found server: {hostname} ({ip}) at {current_city}, {current_country}"
                    )
                    logger.debug(f"  Coordinates: {current_coords}")
                    logger.debug(f"  Distance: {distance:.2f} km")

                    servers.append(
                        ServerInfo(
                            country=current_country,
                            city=current_city,
                            hostname=hostname,
                            protocol=protocol,
                            provider=provider,
                            ownership=ownership,
                            ip=ip,
                            ipv6=ipv6,
                            latitude=current_coords[0],
                            longitude=current_coords[1],
                            distance_km=distance,
                        )
                    )
                else:
                    logger.debug(f"Line did not match server pattern: {line}")

            # Sort servers by distance
            servers.sort(key=lambda x: x.distance_km)

            # Log sorted servers with locations and distances
            logger.debug("\nSorted servers by distance from reference point:")
            logger.debug(
                f"Reference: {self.reference_location} at {self.reference_coords}"
            )
            logger.debug("-" * 60)
            for server in servers:
                logger.debug(
                    f"{server.city}, {server.country}: {server.distance_km:.0f} km ({server.latitude:.4f}, {server.longitude:.4f})"
                )
            logger.debug("-" * 60)

            logger.info(
                f"Successfully parsed and sorted {len(servers)} servers by distance"
            )
            return servers
        except subprocess.CalledProcessError as e:
            logger.error(f"Error getting server list: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error while getting server list: {e}")
            logger.exception(e)
            return []

    def _run_speedtest(self) -> SpeedTestResult:
        """Run speedtest-cli and return results."""
        try:
            logger.info("Running speedtest...")
            s = speedtest.Speedtest()
            s.get_best_server()
            download_speed = s.download() / 1_000_000  # Convert to Mbps
            upload_speed = s.upload() / 1_000_000  # Convert to Mbps
            results = s.results.dict()

            result = SpeedTestResult(
                download_speed=download_speed,
                upload_speed=upload_speed,
                ping=results["ping"],
                jitter=results.get("jitter", 0),
                packet_loss=results.get("packetLoss", 0),
            )

            logger.info(
                f"Speedtest results - Download: {result.download_speed:.2f} Mbps, "
                f"Upload: {result.upload_speed:.2f} Mbps, Ping: {result.ping:.2f} ms"
            )
            return result

        except Exception as e:
            logger.error(f"Error running speedtest: {e}")
            return SpeedTestResult(0, 0, 0, 0, 100)

    def _run_mtr(self) -> MtrResult:
        """Run mtr and return results."""
        try:
            logger.info(f"Running MTR to {self.target_host}...")
            output = subprocess.check_output(
                ["sudo", "mtr", "-n", "-c", "20", "-r", self.target_host],
                text=True,
                timeout=60,
            )

            lines = output.strip().split("\n")[1:]  # Skip header
            if not lines:
                logger.warning("No MTR results received")
                return MtrResult(0, 100, 0)

            last_hop = lines[-1].split()
            avg_latency = float(last_hop[7])  # Average latency
            packet_loss = float(last_hop[2].rstrip("%"))  # Loss%
            hops = len(lines)

            logger.info(
                f"MTR results - Latency: {avg_latency:.2f} ms, "
                f"Packet Loss: {packet_loss:.2f}%, Hops: {hops}"
            )
            return MtrResult(avg_latency, packet_loss, hops)

        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.error(f"Error running mtr: {e}")
            return MtrResult(0, 100, 0)
        except Exception as e:
            logger.error(f"Unexpected error during MTR test: {e}")
            logger.exception(e)
            return MtrResult(0, 100, 0)

    def connect_to_server(self, server: ServerInfo) -> bool:
        """Connect to a specific Mullvad server."""
        try:
            logger.info(
                f"Connecting to server {server.hostname} ({server.city}, {server.country})..."
            )

            connection_start_time = time.time()

            # Set the relay
            subprocess.run(
                ["mullvad", "relay", "set", "location", server.hostname], check=True
            )

            # Connect
            subprocess.run(["mullvad", "connect"], check=True)

            # Wait for connection to establish with polling
            logger.info("Waiting for connection to establish...")
            poll_interval = 0.5  # Check every 0.5 seconds
            elapsed_time = 0

            while elapsed_time < self.max_connection_timeout:
                output = subprocess.check_output(["mullvad", "status"], text=True)
                if "Connected" in output:
                    server.connection_time = time.time() - connection_start_time
                    server.connection_status = "connected"
                    logger.info(
                        f"Successfully connected to server in {server.connection_time:.2f} seconds"
                    )
                    return True

                time.sleep(poll_interval)
                elapsed_time += poll_interval

            logger.warning(
                f"Failed to connect to server after {self.max_connection_timeout} seconds"
            )
            server.connection_time = 0
            server.connection_status = "disconnected"
            return False

        except subprocess.CalledProcessError as e:
            logger.error(f"Error connecting to server {server.hostname}: {e}")
            server.connection_status = "disconnected"
            return False
        except Exception as e:
            logger.error(f"Unexpected error while connecting to server: {e}")
            return False

    def test_server(self, server: ServerInfo) -> Tuple[SpeedTestResult, MtrResult]:
        """Test a single server's performance."""
        if not self.connect_to_server(server):
            logger.warning(
                f"Skipping tests for {server.hostname} due to connection failure"
            )
            return SpeedTestResult(0, 0, 0, 0, 100), MtrResult(0, 100, 0)

        speedtest_result = self._run_speedtest()
        mtr_result = self._run_mtr()

        return speedtest_result, mtr_result

    def run_tests(self, protocol: str = "WireGuard", max_servers: int = None):
        """Run tests on all servers or up to max_servers."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        results_file = f"mullvad_test_results_{timestamp}_{protocol.lower()}.log"
        if protocol == "WireGuard":
            protocol = "wg"
        else:
            protocol = "ovpn"

        filtered_servers = [
            s for s in self.servers if protocol.lower() in s.protocol.lower()
        ]

        if max_servers:
            filtered_servers = filtered_servers[:max_servers]

        if RANDOM_SELECTION:
            random.shuffle(filtered_servers)
        if not filtered_servers:
            logger.error(f"No servers found for protocol: {protocol}")
            return

        logger.info(
            f"Starting tests on {len(filtered_servers)} servers with protocol {protocol}"
        )

        with open(results_file, "w") as f:
            f.write("Mullvad VPN Server Performance Test Results\n")
            f.write(f"Test Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Reference Location: {self.reference_location}\n")
            f.write(f"Target Host for MTR: {self.target_host}\n")
            f.write(f"Protocol: {protocol}\n")
            f.write("=" * 80 + "\n\n")

            for idx, server in enumerate(filtered_servers, 1):
                logger.info(
                    f"\nTesting server {idx}/{len(filtered_servers)}: {server.hostname}"
                )
                print(
                    f"\nTesting server {idx}/{len(filtered_servers)}: {server.hostname}"
                )
                print(
                    f"Location: {server.city}, {server.country} (Distance: {server.distance_km:.0f} km)"
                )

                speedtest_result, mtr_result = self.test_server(server)
                self.results[server.hostname] = (speedtest_result, mtr_result)

                if server.connection_status == "connected":
                    f.write(f"Server: {server.hostname}\n")
                    f.write(f"Location: {server.city}, {server.country}\n")
                    f.write(f"Distance: {server.distance_km:.0f} km\n")
                    f.write(f"Provider: {server.provider} ({server.ownership})\n")
                    f.write(f"Protocol: {server.protocol}\n")
                    f.write(f"Connection Time: {server.connection_time:.2f} seconds\n")
                    f.write("\nSpeedtest Results:\n")
                    f.write(f"Download: {speedtest_result.download_speed:.2f} Mbps\n")
                    f.write(f"Upload: {speedtest_result.upload_speed:.2f} Mbps\n")
                    f.write(f"Ping: {speedtest_result.ping:.2f} ms\n")
                    f.write(f"Jitter: {speedtest_result.jitter:.2f} ms\n")
                    f.write(f"Packet Loss: {speedtest_result.packet_loss:.2f}%\n")
                    f.write("\nMTR Results:\n")
                    f.write(f"Average Latency: {mtr_result.avg_latency:.2f} ms\n")
                    f.write(f"Packet Loss: {mtr_result.packet_loss:.2f}%\n")
                    f.write(f"Number of Hops: {mtr_result.hops}\n")
                    f.write("=" * 80 + "\n\n")

        if self.results:
            self._print_summary(results_file)
        else:
            logger.error("No test results available to generate summary")

    def _print_summary(self, results_file: str):
        """Print a summary of the best performing servers."""
        if not self.results:
            logger.error("No results available for summary")
            return

        try:
            # Sort servers by different metrics
            servers_by_distance = sorted(
                [
                    (s.hostname, s.distance_km)
                    for s in self.servers
                    if s.hostname in self.results and s.connection_status == "connected"
                ],
                key=lambda x: x[1],
            )

            servers_by_download = sorted(
                [
                    s.hostname
                    for s in self.servers
                    if s.hostname in self.results and s.connection_status == "connected"
                ],
                key=lambda hostname: self.results[hostname][0].download_speed,
                reverse=True,
            )

            servers_by_upload = sorted(
                [
                    s.hostname
                    for s in self.servers
                    if s.hostname in self.results and s.connection_status == "connected"
                ],
                key=lambda hostname: self.results[hostname][0].upload_speed,
                reverse=True,
            )

            servers_by_latency = sorted(
                [
                    s.hostname
                    for s in self.servers
                    if s.hostname in self.results and s.connection_status == "connected"
                ],
                key=lambda hostname: (
                    self.results[hostname][1].avg_latency
                    if self.results[hostname][1].avg_latency > 0
                    else float("inf")
                ),
            )

            servers_by_packet_loss = sorted(
                [
                    s.hostname
                    for s in self.servers
                    if s.hostname in self.results and s.connection_status == "connected"
                ],
                key=lambda hostname: (
                    self.results[hostname][0].packet_loss
                    + self.results[hostname][1].packet_loss
                ),
            )

            servers_by_connection_time = sorted(
                [
                    s.hostname
                    for s in self.servers
                    if s.hostname in self.results and s.connection_status == "connected"
                ],
                key=lambda hostname: next(
                    s.connection_time
                    for s in self.servers
                    if s.hostname == hostname and s.connection_time > 0
                ),
            )

            with open(results_file, "a") as f:
                f.write("\nSUMMARY\n")
                f.write("=" * 80 + "\n\n")

                f.write(f"Reference Location: {self.reference_location}\n\n")

                f.write("Top Servers by Distance:\n")
                for hostname, distance in servers_by_distance[:TOP_SERVERS_NUM]:
                    server = next(s for s in self.servers if s.hostname == hostname)
                    f.write(
                        f"{hostname} ({server.city}, {server.country}): {distance:.0f} km\n"
                    )

                f.write("\nTop Servers by Connection Speed:\n")
                for hostname in servers_by_connection_time[:TOP_SERVERS_NUM]:
                    server = next(s for s in self.servers if s.hostname == hostname)
                    if server.connection_time > 0:
                        f.write(f"{hostname}: {server.connection_time:.2f} seconds\n")

                f.write("\nTop Servers by Download Speed:\n")
                for hostname in servers_by_download[:TOP_SERVERS_NUM]:
                    f.write(
                        f"{hostname}: {self.results[hostname][0].download_speed:.2f} Mbps\n"
                    )

                f.write("\nTop Servers by Upload Speed:\n")
                for hostname in servers_by_upload[:TOP_SERVERS_NUM]:
                    f.write(
                        f"{hostname}: {self.results[hostname][0].upload_speed:.2f} Mbps\n"
                    )

                f.write("\nTop Servers by Latency:\n")
                for hostname in servers_by_latency[:TOP_SERVERS_NUM]:
                    f.write(
                        f"{hostname}: {self.results[hostname][1].avg_latency:.2f} ms\n"
                    )

                f.write("\nTop Servers by Reliability (Lowest Packet Loss):\n")
                for hostname in servers_by_packet_loss[:TOP_SERVERS_NUM]:
                    total_loss = (
                        self.results[hostname][0].packet_loss
                        + self.results[hostname][1].packet_loss
                    )
                    f.write(f"{hostname}: {total_loss:.2f}% packet loss\n")

                # Calculate averages only for servers with valid results
                valid_results = [
                    (s, m)
                    for s, m in self.results.values()
                    if s.download_speed > 0 and m.avg_latency > 0
                ]

                if valid_results:
                    avg_download = statistics.mean(
                        r[0].download_speed for r in valid_results
                    )
                    avg_upload = statistics.mean(
                        r[0].upload_speed for r in valid_results
                    )
                    avg_latency = statistics.mean(
                        r[1].avg_latency for r in valid_results
                    )

                    # Calculate average connection time for successful connections
                    successful_connections = [
                        s.connection_time for s in self.servers if s.connection_time > 0
                    ]
                    avg_connection_time = (
                        statistics.mean(successful_connections)
                        if successful_connections
                        else 0
                    )

                    f.write("\nOverall Statistics (excluding failed tests):\n")
                    f.write(
                        f"Average Connection Time: {avg_connection_time:.2f} seconds\n"
                    )
                    f.write(f"Average Download Speed: {avg_download:.2f} Mbps\n")
                    f.write(f"Average Upload Speed: {avg_upload:.2f} Mbps\n")
                    f.write(f"Average Latency: {avg_latency:.2f} ms\n")
                else:
                    f.write("\nNo valid test results available for statistics\n")

        except Exception as e:
            logger.error(f"Error generating summary: {e}")


def main():
    parser = argparse.ArgumentParser(description="Test Mullvad VPN servers performance")
    parser.add_argument(
        "--location",
        type=str,
        default=DEFAULT_LOCATION,
        help=f"Reference location for distance calculation (default: {DEFAULT_LOCATION})",
    )
    parser.add_argument(
        "--protocol",
        type=str,
        default="WireGuard",
        choices=["WireGuard", "OpenVPN"],
        help="VPN protocol to test (default: WireGuard)",
    )
    parser.add_argument(
        "--max-servers",
        type=int,
        default=MAX_SERVERS_TO_TEST,
        help=f"Maximum number of servers to test (default: {MAX_SERVERS_TO_TEST})",
    )

    args = parser.parse_args()

    try:
        # Check if speedtest-cli is installed
        subprocess.run(["speedtest-cli", "--version"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        logger.error(
            "speedtest-cli is not installed. Please install it using: pip install speedtest-cli"
        )
        sys.exit(1)

    try:
        # Check if mtr is installed
        subprocess.run(["mtr", "--version"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        logger.error(
            "mtr is not installed. Please install it using your package manager"
        )
        sys.exit(1)

    try:
        # Check if mullvad is installed and accessible
        subprocess.run(["mullvad", "--version"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        logger.error("Mullvad CLI is not installed or not accessible")
        sys.exit(1)

    # Create tester instance with reference location
    tester = MullvadTester(reference_location=args.location)

    # Run tests
    tester.run_tests(protocol=args.protocol, max_servers=args.max_servers)


if __name__ == "__main__":
    main()
