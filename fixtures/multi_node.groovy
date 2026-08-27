node('linux') {
    stage('Build on Linux') {
        sh './build.sh'
    }
}
node('windows') {
    stage('Build on Windows') {
        bat 'build.bat'
    }
}
