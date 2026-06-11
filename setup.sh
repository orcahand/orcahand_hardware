#!/bin/bash

echo "🚀 Setting up ORCA Files Sync System..."

# Check if Node.js is installed
if ! command -v node &> /dev/null; then
    echo "❌ Node.js is not installed. Please install Node.js 18 or higher."
    echo "   Visit: https://nodejs.org/"
    exit 1
fi

# Check Node.js version
NODE_VERSION=$(node --version | cut -d'v' -f2 | cut -d'.' -f1)
if [ "$NODE_VERSION" -lt 18 ]; then
    echo "❌ Node.js version $NODE_VERSION is too old. Please install Node.js 18 or higher."
    exit 1
fi

echo "✅ Node.js $(node --version) detected"

# Check if npm is installed
if ! command -v npm &> /dev/null; then
    echo "❌ npm is not installed. Please install npm."
    exit 1
fi

echo "✅ npm $(npm --version) detected"

# Install dependencies
echo "📦 Installing dependencies..."
npm install

if [ $? -eq 0 ]; then
    echo "✅ Dependencies installed successfully"
else
    echo "❌ Failed to install dependencies"
    exit 1
fi

# Make scripts executable
chmod +x scripts/sync.js
chmod +x scripts/sync-all.js

echo "✅ Scripts made executable"

# Test Firebase connection (dry run)
echo "🔥 Testing Firebase connection..."
node -e "
const { storage } = require('./scripts/firebase-config');
console.log('Firebase Storage initialized successfully');
console.log('Storage bucket:', storage.app.options.storageBucket);
"

if [ $? -eq 0 ]; then
    echo "✅ Firebase connection test passed"
else
    echo "⚠️  Firebase connection test failed - check your configuration"
fi

# Create logs directory if it doesn't exist
mkdir -p logs

echo ""
echo "🎉 Setup completed successfully!"
echo ""
echo "Next steps:"
echo "1. Make changes to files in any of the tracked folders"
echo "2. Commit and push to main branch to trigger automatic sync"
echo "3. Or run 'npm run sync' to upload locally"
echo ""
echo "For help, see the README.md file or run:"
echo "  npm run sync --help" 