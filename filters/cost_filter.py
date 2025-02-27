"""
title: Live Cost Tracker when Chatting
authors: brammittendorff
author_url: https://github.com/brammittendorff/openwebui-pipelines
funding_url: https://github.com/open-webui
version: 0.0.5
requirements: requests, tiktoken, pydantic
required_open_webui_version: 0.3.20
license: MIT
"""

import json
import os
import sqlite3
import time
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from threading import Lock
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

import requests
import tiktoken
from pydantic import BaseModel, Field

# Import OpenWebUI utilities if available, otherwise use our own implementations
try:
    from open_webui.utils.misc import get_last_assistant_message, get_messages_content
except ImportError:
    # Fallback implementations if the imports fail
    def get_messages_content(messages):
        """Extract text content from messages"""
        content = ""
        for message in messages:
            if isinstance(message, dict) and "content" in message:
                if isinstance(message["content"], str):
                    content += message["content"] + "\n"
                elif isinstance(message["content"], list):
                    # Handle content as a list of parts (multimodal messages)
                    for part in message["content"]:
                        if isinstance(part, dict) and "text" in part:
                            content += part["text"] + "\n"
        return content

    def get_last_assistant_message(messages):
        """Get the last assistant message from the conversation"""
        for message in reversed(messages):
            if (
                isinstance(message, dict)
                and message.get("role") == "assistant"
                and "content" in message
            ):
                if isinstance(message["content"], str):
                    return message["content"]
                elif isinstance(message["content"], list):
                    # Handle content as a list of parts
                    text_parts = []
                    for part in message["content"]:
                        if isinstance(part, dict) and "text" in part:
                            text_parts.append(part["text"])
                    return "\n".join(text_parts)
        return ""


class Filter:
    """
    Cost Tracking Filter for Open WebUI

    This filter tracks token usage and calculates costs for different AI models
    based on pricing information from LiteLLM's GitHub repository.
    """

    class Valves(BaseModel):
        """Configuration options for the cost tracker filter"""

        priority: int = Field(default=15, description="Priority level for the filter")
        compensation: float = Field(
            default=1.0,
            description="Compensation multiplier for cost calculation (percent)",
        )
        elapsed_time: bool = Field(
            default=True, description="Display elapsed processing time"
        )
        number_of_tokens: bool = Field(
            default=True, description="Display total number of tokens"
        )
        tokens_per_sec: bool = Field(
            default=True, description="Display tokens per second metric"
        )
        debug: bool = Field(default=False, description="Enable debug logging")
        pricing_url: str = Field(
            default="https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
            description="URL for fetching model pricing data",
        )
        default_input_cost: float = Field(
            default=0.000001,
            description="Default cost per input token if model not found",
        )
        default_output_cost: float = Field(
            default=0.000005,
            description="Default cost per output token if model not found",
        )
        cache_ttl: int = Field(
            default=432000,  # 5 days in seconds
            description="Time to live for cache in seconds",
        )

    def __init__(self):
        """Initialize the cost tracker filter"""
        # Initialize configuration
        self.valves = self.Valves()
        self.debug = self.valves.debug

        # Set up paths
        self.data_dir = "data"
        self.cache_dir = os.path.join(self.data_dir, ".cache")
        self.db_path = os.path.join(self.data_dir, "costs.db")

        # Ensure directories exist
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        # Initialize database
        self._init_database()

        # Load pricing data - only basic initialization here
        self.pricing_lock = Lock()
        self.pricing_cache_file = os.path.join(self.cache_dir, "model_pricing.json")

        # Map for common model prefixes to help with matching
        self.model_prefixes = {
            "claude": "anthropic",
            "gpt": "openai",
            "mistral": "mistral",
            "llama": "meta",
            "gemma": "google",
            "gemini": "google",
            "command": "cohere",
            "palm": "google",
            "phi": "microsoft",
        }

        # Runtime variables
        self.start_time = None
        self.input_tokens = 0
        self.model_name = "unknown"

    def _log(self, message):
        """Log debug messages if debug is enabled"""
        if self.debug:
            with open("cost_tracker_debug.log", "a") as f:
                f.write(f"{datetime.now().isoformat()} - {message}\n")
            print(f"DEBUG: Cost Tracker - {message}")

    def _init_database(self):
        """Initialize the costs database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Create model_prices table (simplified)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS model_prices (
                model_name TEXT PRIMARY KEY,
                input_cost_per_token REAL NOT NULL,
                output_cost_per_token REAL NOT NULL,
                last_updated INTEGER NOT NULL
            )
        """
        )

        # Create cost_tracking table (simplified)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS cost_tracking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                model TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                total_cost TEXT NOT NULL
            )
        """
        )

        conn.commit()
        conn.close()

    def _normalize_model_name(self, model_name):
        """Normalize model name for better matching"""
        if not model_name:
            return "unknown"

        # Convert to lowercase and strip whitespace
        model_name = model_name.lower().strip()

        # Remove common prefixes
        prefixes = [
            "anthropic/",
            "anthropic.",
            "openai/",
            "openai.",
            "google/",
            "google.",
            "mistral/",
            "mistral.",
            "azure/",
            "azure.",
            "cohere/",
            "cohere.",
            "meta/",
            "meta.",
            "llama/",
            "llama.",
            "microsoft/",
            "microsoft.",
        ]

        for prefix in prefixes:
            if model_name.startswith(prefix):
                model_name = model_name[len(prefix) :]
                break

        # Remove version timestamps and common suffixes
        # Handle date-based suffixes which are common in model naming
        date_patterns = [
            r"-\d{4}-\d{2}-\d{2}",  # Format: -YYYY-MM-DD
            r"-\d{8}",  # Format: -YYYYMMDD
            r"-\d{6}",  # Format: -YYYYMM
            r"-\d{4}\d{2}$",  # Format: -YYYYMM at the end
        ]

        for pattern in date_patterns:
            import re

            model_name = re.sub(pattern, "", model_name)

        # Remove other common suffixes
        suffixes = [
            "-latest",
            "-preview",
            "-beta",
            "-alpha",
            "-vision",
            "-audio",
            "-instruct",
            "-audio-preview",
        ]

        for suffix in suffixes:
            if model_name.endswith(suffix):
                model_name = model_name[: -len(suffix)]
                break

        return model_name

    def _get_model_pricing(self, model_name):
        """Get pricing data for a model using a simplified approach"""
        normalized_name = self._normalize_model_name(model_name)
        self._log(f"Looking for pricing data for: {normalized_name}")

        # Try to find in database first (most efficient)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 1. Try exact match
        cursor.execute(
            "SELECT input_cost_per_token, output_cost_per_token FROM model_prices WHERE model_name = ?",
            (normalized_name,),
        )
        row = cursor.fetchone()
        if row:
            input_cost, output_cost = row
            conn.close()
            self._log(f"Found exact match in database: {normalized_name}")
            return {
                "input_cost_per_token": input_cost,
                "output_cost_per_token": output_cost,
            }

        # 2. Try prefix-based match (e.g., "claude-3" matches "claude-3-sonnet")
        cursor.execute(
            "SELECT model_name, input_cost_per_token, output_cost_per_token FROM model_prices WHERE model_name LIKE ?",
            (f"{normalized_name}%",),
        )
        rows = cursor.fetchall()
        if rows:
            # Use the first match
            model_name, input_cost, output_cost = rows[0]
            conn.close()
            self._log(f"Found prefix match in database: {model_name}")
            return {
                "input_cost_per_token": input_cost,
                "output_cost_per_token": output_cost,
            }

        # 3. Try model family match (e.g., "claude" for any Claude model)
        model_parts = normalized_name.split("-")
        if model_parts:
            model_family = model_parts[0]
            cursor.execute(
                "SELECT model_name, input_cost_per_token, output_cost_per_token FROM model_prices WHERE model_name LIKE ?",
                (f"{model_family}%",),
            )
            rows = cursor.fetchall()
            if rows:
                # Use the first match
                model_name, input_cost, output_cost = rows[0]
                conn.close()
                self._log(f"Found family match in database: {model_name}")
                return {
                    "input_cost_per_token": input_cost,
                    "output_cost_per_token": output_cost,
                }

        # 4. If we got here, we need to update our pricing data and try again
        conn.close()
        self._log(f"No match found in database, fetching new pricing data")

        # Try to update pricing data
        if self._update_pricing_data():
            # Try one more time with the updated data
            return self._get_model_pricing(model_name)

        # 5. Last resort: use default values
        self._log(f"Using default pricing for {normalized_name}")
        return {
            "input_cost_per_token": self.valves.default_input_cost,
            "output_cost_per_token": self.valves.default_output_cost,
        }

    def _update_pricing_data(self):
        """Update pricing data from API if needed"""
        try:
            # Check if we need to update (cache expired or doesn't exist)
            should_update = True
            if os.path.exists(self.pricing_cache_file):
                cache_age = time.time() - os.path.getmtime(self.pricing_cache_file)
                should_update = cache_age > self.valves.cache_ttl

            if not should_update:
                self._log("Using existing pricing data cache")
                return self._load_pricing_from_cache()

            # Fetch new data
            self._log(f"Fetching pricing data from: {self.valves.pricing_url}")
            response = requests.get(self.valves.pricing_url, timeout=10)
            response.raise_for_status()
            pricing_data = response.json()

            # Filter out non-model entries
            if "sample_spec" in pricing_data:
                del pricing_data["sample_spec"]

            # Save to cache
            with self.pricing_lock:
                with open(self.pricing_cache_file, "w") as f:
                    json.dump(pricing_data, f)

            # Update database
            self._update_database_from_pricing_data(pricing_data)

            return True
        except Exception as e:
            self._log(f"Error updating pricing data: {e}")
            return self._load_pricing_from_cache()  # Try to use cache as fallback

    def _load_pricing_from_cache(self):
        """Load pricing data from cache file"""
        try:
            if os.path.exists(self.pricing_cache_file):
                with open(self.pricing_cache_file, "r") as f:
                    pricing_data = json.load(f)
                    self._update_database_from_pricing_data(pricing_data)
                    return True
        except Exception as e:
            self._log(f"Error loading pricing cache: {e}")
        return False

    def _update_database_from_pricing_data(self, pricing_data):
        """Update database with pricing data"""
        if not pricing_data:
            return False

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        timestamp = int(time.time())
        count = 0

        try:
            # Begin transaction
            conn.execute("BEGIN TRANSACTION")

            for model_name, model_data in pricing_data.items():
                # Skip non-model entries or sample specs
                if model_name == "sample_spec" or not model_name:
                    continue

                # Extract standard text token pricing (required fields)
                input_cost = model_data.get("input_cost_per_token")
                output_cost = model_data.get("output_cost_per_token")

                # Skip if both costs are missing or zero
                if not input_cost and not output_cost:
                    continue

                # Use defaults if either is missing
                input_cost = (
                    input_cost
                    if input_cost is not None
                    else self.valves.default_input_cost
                )
                output_cost = (
                    output_cost
                    if output_cost is not None
                    else self.valves.default_output_cost
                )

                # Normalize model name
                norm_model_name = self._normalize_model_name(model_name)

                # Insert or update model pricing
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO model_prices 
                    (model_name, input_cost_per_token, output_cost_per_token, last_updated) 
                    VALUES (?, ?, ?, ?)
                    """,
                    (norm_model_name, input_cost, output_cost, timestamp),
                )
                count += 1

                # Also add base model entries for models with version numbers
                # This helps with matching newer versions to known base models
                base_parts = norm_model_name.split("-")
                if len(base_parts) > 1:
                    base_model = base_parts[0]
                    # Only add base model if it doesn't exist yet
                    cursor.execute(
                        "SELECT 1 FROM model_prices WHERE model_name = ?", (base_model,)
                    )
                    if not cursor.fetchone():
                        cursor.execute(
                            """
                            INSERT OR IGNORE INTO model_prices 
                            (model_name, input_cost_per_token, output_cost_per_token, last_updated) 
                            VALUES (?, ?, ?, ?)
                            """,
                            (base_model, input_cost, output_cost, timestamp),
                        )

            # Commit transaction
            conn.commit()
            self._log(f"Updated pricing database with {count} models")
            return True
        except Exception as e:
            self._log(f"Error updating price database: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def _sanitize_model_name(self, name: str) -> str:
        """Sanitize model name by removing prefixes and suffixes"""
        if not name:
            return "unknown"
            
        prefixes = [
            "openai",
            "github",
            "google_genai",
        ]
        suffixes = ["-tuned"]
        # remove prefixes and suffixes
        for prefix in prefixes:
            if name.startswith(prefix):
                name = name[len(prefix) :]
        for suffix in suffixes:
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        return name.lower().strip()

    def _get_model(self, body):
        """Get the model name from the request body"""
        if "model" in body:
            return self._sanitize_model_name(body["model"])
        return "unknown"

    def _calculate_cost(self, model, input_tokens, output_tokens):
        """Calculate cost based on token counts and model pricing"""
        # Get pricing data
        pricing = self._get_model_pricing(model)

        # Get cost rates
        input_cost_per_token = Decimal(
            str(pricing.get("input_cost_per_token", self.valves.default_input_cost))
        )
        output_cost_per_token = Decimal(
            str(pricing.get("output_cost_per_token", self.valves.default_output_cost))
        )

        # Calculate costs
        input_cost = input_tokens * input_cost_per_token
        output_cost = output_tokens * output_cost_per_token
        total_cost = Decimal(str(self.valves.compensation)) * (input_cost + output_cost)

        # Round to 8 decimal places
        return total_cost.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)

    def _save_cost_record(
        self, user_id, model, input_tokens, output_tokens, total_cost
    ):
        """Save a cost tracking record to the database"""
        timestamp = datetime.now().isoformat()

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute(
                """INSERT INTO cost_tracking 
                   (user_id, model, timestamp, input_tokens, output_tokens, total_cost) 
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    model,
                    timestamp,
                    input_tokens,
                    output_tokens,
                    str(total_cost),
                ),
            )

            conn.commit()
            conn.close()
            return True
        except Exception as e:
            self._log(f"Error saving cost record: {e}")
            return False

    def _remove_roles(self, content):
        """Remove role prefixes from content"""
        # Define the roles to be removed
        roles = ["SYSTEM:", "USER:", "ASSISTANT:", "PROMPT:"]

        # Process each line
        def process_line(line):
            for role in roles:
                if line.startswith(role):
                    return line.split(":", 1)[1].strip()
            return line  # Return the line unchanged if no role matches

        return "\n".join([process_line(line) for line in content.split("\n")])

    async def inlet(
        self,
        body: dict,
        __event_emitter__: Callable[[Any], Awaitable[None]] = None,
        __model__: Optional[dict] = None,
        __user__: Optional[dict] = None,
    ) -> dict:
        """Process incoming request before sending to LLM"""
        self.debug = self.valves.debug
        self._log("Inlet called")

        # Get input tokens using a standard encoding
        enc = tiktoken.get_encoding("cl100k_base")
        
        # Use the actual OpenWebUI function or our fallback
        input_content = self._remove_roles(get_messages_content(body["messages"])).strip()
        self.input_tokens = len(enc.encode(input_content))
        
        # Record model name for later use
        self.model_name = self._get_model(body)

        # Emit status if event emitter is available
        if __event_emitter__:
            try:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": f"Processing {self.input_tokens} input tokens...",
                            "done": False,
                        },
                    }
                )
                self._log(f"Sent status update: Processing {self.input_tokens} input tokens")
            except Exception as e:
                self._log(f"Error sending status update: {e}")

        # add user email to payload in order to track costs
        if __user__ and "email" in __user__:
            self._log(f"Adding email to request body: {__user__['email']}")
            body["user"] = __user__["email"]
        
        # Start timing
        self.start_time = time.time()
        
        return body

    async def outlet(
        self,
        body: dict,
        __event_emitter__: Callable[[Any], Awaitable[None]],
        __model__: Optional[dict] = None,
        __user__: Optional[dict] = None,
    ) -> dict:
        """Process response after receiving from LLM"""
        self._log("Outlet called")
        
        # Calculate elapsed time
        end_time = time.time()
        elapsed_time = end_time - self.start_time

        # Send status update for computing output tokens
        await __event_emitter__(
            {
                "type": "status",
                "data": {
                    "description": "Computing number of output tokens...",
                    "done": False,
                },
            }
        )

        # Get output tokens using a standard encoding
        enc = tiktoken.get_encoding("cl100k_base")
        output_text = get_last_assistant_message(body["messages"])
        output_tokens = len(enc.encode(output_text))
        self._log(f"Output tokens: {output_tokens}")

        # Send status update for computing costs
        await __event_emitter__(
            {
                "type": "status",
                "data": {"description": "Computing total costs...", "done": False},
            }
        )
        
        # Get model and calculate total cost
        model = self._get_model(body)  
        total_cost = self._calculate_cost(
            model, self.input_tokens, output_tokens, 
        )
        self._log(f"Calculated cost: ${total_cost}")

        # Save cost record if user info is available
        user_email = None
        if __user__ and "email" in __user__:
            user_email = __user__["email"]
            try:
                self._save_cost_record(
                    user_email,
                    model,
                    self.input_tokens,
                    output_tokens,
                    total_cost,
                )
                self._log(f"Saved cost record for user email: {user_email}")
            except Exception as e:
                self._log(f"Error saving cost record: {e}")
        
        # Calculate stats
        total_tokens = self.input_tokens + output_tokens
        tokens_per_sec = total_tokens / elapsed_time if elapsed_time > 0 else 0

        # Build stats string
        stats_array = []

        if self.valves.elapsed_time:
            stats_array.append(f"{elapsed_time:.2f} s")
        if self.valves.tokens_per_sec:
            stats_array.append(f"{tokens_per_sec:.2f} T/s")
        if self.valves.number_of_tokens:
            stats_array.append(f"{total_tokens} Tokens")  # Note: "Tokens" with capital T

        # Format cost with exact format from working example
        if float(total_cost) < 0.01:
            stats_array.append(f"${total_cost:.6f}")
        else:
            stats_array.append(f"${total_cost:.2f}")

        # Join with pipe separator
        stats = " | ".join(stats_array)
        self._log(f"Final stats: {stats}")

        # Send final status to UI
        await __event_emitter__(
            {"type": "status", "data": {"description": stats, "done": True}}
        )
        self._log("Sent final status with done=True")
        
        return body